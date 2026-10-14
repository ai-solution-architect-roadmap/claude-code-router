"""Build / incrementally update .ccr/graph.db."""
from __future__ import annotations

import contextlib
import logging
import os
import stat as stat_mod
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ccr.graph import db as gdb
from ccr.graph import git as ggit
from ccr.graph.files import (AUX_NAMES, family_of, in_skipped_dir, is_code, is_test_path,
                             walk_files)
from ccr.graph.parse import init_worker, parse_file
from ccr.graph.resolve import Resolver

log = logging.getLogger("ccr.graph")

PARALLEL_MIN = 200       # parse in a process pool above this many files
CHURN_DAYS = 180


@dataclass
class UpdateResult:
    mode: str = "fs"                 # git | fs
    full: bool = False               # fresh database
    scanned: int = 0                 # code files discovered
    checked: int = 0                 # files read and hashed
    parsed: int = 0                  # files actually (re)parsed
    unchanged: int = 0               # hashed but content identical
    added: int = 0
    removed: int = 0
    parsed_files: list[str] = field(default_factory=list)
    parsers: dict[str, int] = field(default_factory=dict)
    churn_updated: bool = False
    commit: str | None = None
    elapsed: float = 0.0


# --------------------------------------------------------------------------- helpers
def _ts_languages(rels: list[str], offline: bool) -> tuple[dict[str, str | None], frozenset[str]]:
    """Map rel -> tree-sitter language name and return the set usable without network."""
    try:
        import tree_sitter_language_pack as tslp
    except Exception as e:  # pragma: no cover - broken install
        log.debug("tree-sitter-language-pack unavailable: %s", e)
        return {r: None for r in rels}, frozenset()
    langs: dict[str, str | None] = {}
    for r in rels:
        try:
            langs[r] = tslp.detect_language_from_path(r)
        except Exception:
            log.debug("language detection failed for %s", r, exc_info=True)
            langs[r] = None
    try:
        have = set(tslp.downloaded_languages())
    except Exception:
        log.debug("could not list downloaded grammars", exc_info=True)
        have = set()
    needed = {lang for lang in langs.values() if lang} - have
    if needed and not offline:
        try:
            tslp.download(sorted(needed))
        except Exception as e:
            log.debug("grammar download failed (%s); falling back to regex", e)
        with contextlib.suppress(Exception):
            have = set(tslp.downloaded_languages())
    return langs, frozenset(have)


def _parse_all(jobs: list[tuple], allowed: frozenset[str], workers: int | None) -> list[dict]:
    if not jobs:
        return []
    n = workers if workers is not None else min(8, os.cpu_count() or 1)
    if n > 1 and len(jobs) >= PARALLEL_MIN:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=n, initializer=init_worker,
                                     initargs=(allowed,)) as ex:
                return list(ex.map(parse_file, jobs, chunksize=max(8, len(jobs) // (n * 8))))
        except Exception as e:  # pool cannot start (sandbox, frozen app): go sequential
            log.debug("process pool failed (%s); parsing sequentially", e)
    init_worker(allowed)
    return [parse_file(j) for j in jobs]


def _temp_ids(conn, name: str, ids) -> None:
    conn.execute(f"DROP TABLE IF EXISTS temp.{name}")
    conn.execute(f"CREATE TEMP TABLE {name}(v PRIMARY KEY) WITHOUT ROWID")
    conn.executemany(f"INSERT OR IGNORE INTO temp.{name} VALUES(?)", ((i,) for i in ids))


def _purge_file(conn, fid: int, names: set[str], drop_file: bool) -> None:
    for (nm,) in conn.execute("SELECT name FROM nodes WHERE file_id = ?", (fid,)):
        names.add(nm)
    conn.execute("DELETE FROM edges WHERE src IN (SELECT id FROM nodes WHERE file_id = ?) "
                 "OR dst IN (SELECT id FROM nodes WHERE file_id = ?)", (fid, fid))
    conn.execute("DELETE FROM edges WHERE src = ?", (fid,))
    conn.execute("DELETE FROM refs WHERE file_id = ?", (fid,))
    conn.execute("DELETE FROM imports WHERE file_id = ?", (fid,))
    conn.execute("DELETE FROM nodes WHERE file_id = ?", (fid,))
    if drop_file:
        conn.execute("DELETE FROM edges WHERE dst = ?", (fid,))
        conn.execute("UPDATE imports SET resolved_id = NULL WHERE resolved_id = ?", (fid,))
        conn.execute("DELETE FROM nodes WHERE id = ?", (fid,))


def _read_aux(root: Path, rels: list[str]) -> dict[str, str]:
    out = {}
    for r in rels:
        try:
            out[r] = (root / r).read_text(encoding="utf-8", errors="ignore")[:20000]
        except OSError:
            continue
    return out


# Families where code in the same directory shares a namespace without imports.
SAME_DIR_NS = frozenset({"go", "jvm", "csharp", "swift"})
# Families with a global namespace (includes/requires load everything); unique names link.
GLOBAL_NS = frozenset({"c", "ruby", "php", "shell", "elixir", "lua", "dart", "swift", "csharp"})
# Generic method names: a qualified x.close() across files is too ambiguous to link by name.
COMMON_METHODS = frozenset("""
open close get set put post delete patch run start stop read write update add append extend
insert remove pop push clear copy keys values items join split strip format execute fetch
fetchone fetchall commit rollback send recv call apply bind map filter reduce find index count
sort reverse load save dump dumps loads parse render init setup teardown reset next iter len str
repr eq hash then catch finally emit on off subscribe toString equals hashCode valueOf length size
contains has exists list create build make new error info debug warn warning log print
""".split())


def _load_call_inputs(conn, names: set[str] | None):
    """Refs to resolve and the candidate symbols for them (deleting stale call edges)."""
    cols = "r.id, r.file_id, r.caller_id, r.name, r.line, r.qual, f.path"
    base = f"SELECT {cols} FROM refs r JOIN nodes f ON f.id = r.file_id"
    if names is None:
        conn.execute("DELETE FROM edges WHERE kind = 'calls'")
        refs = conn.execute(base).fetchall()
        syms = conn.execute("SELECT id, file_id, name, kind, path FROM nodes "
                            "WHERE kind != 'file'").fetchall()
        return refs, syms
    _temp_ids(conn, "aff_names", names)
    conn.execute("DELETE FROM edges WHERE kind = 'calls' AND ref_id IN "
                 "(SELECT id FROM refs WHERE name IN (SELECT v FROM temp.aff_names))")
    refs = conn.execute(base + " WHERE r.name IN (SELECT v FROM temp.aff_names)").fetchall()
    syms = conn.execute("SELECT id, file_id, name, kind, path FROM nodes WHERE kind != 'file' "
                        "AND name IN (SELECT v FROM temp.aff_names)").fetchall()
    return refs, syms


def _import_maps(conn, refs, names: set[str] | None):
    """file -> files it imports, and file -> modules reached through barrel re-exports."""
    imports: dict[int, set[int]] = defaultdict(set)
    sql = "SELECT file_id, resolved_id FROM imports WHERE resolved_id IS NOT NULL"
    if names is not None:
        _temp_ids(conn, "ref_files", {r[1] for r in refs})
        sql += " AND file_id IN (SELECT v FROM temp.ref_files)"
    for fid, rid in conn.execute(sql):
        imports[fid].add(rid)
    # one hop through barrel files (__init__.py, index.ts, mod.rs) that re-export modules
    barrels = {i for (i,) in conn.execute(
        "SELECT id FROM nodes WHERE kind = 'file' AND (name LIKE '\\_\\_init\\_\\_.%' ESCAPE '\\' "
        "OR name LIKE 'index.%' OR name = 'mod.rs')")}
    used = {b for targets in imports.values() for b in targets if b in barrels}
    reexports: dict[int, set[int]] = defaultdict(set)
    if used:
        _temp_ids(conn, "barrels", used)
        via: dict[int, set[int]] = defaultdict(set)
        for b, rid in conn.execute("SELECT file_id, resolved_id FROM imports WHERE resolved_id "
                                   "IS NOT NULL AND file_id IN (SELECT v FROM temp.barrels)"):
            via[b].add(rid)
        for fid, targets in imports.items():
            for b in targets:
                reexports[fid] |= via.get(b, set())
    return imports, reexports


def _pick(entries, qual: int) -> list[int]:
    return [sid for sid, is_m in entries if qual or not is_m]


def _callee_hits(fid: int, nm: str, qual: int, rpath: str, files: dict, by_dir: dict,
                 plain: dict, imports: dict, reexports: dict) -> list[int]:
    """Symbols a call to `nm` can reach, by tier: caller's file, imported files, barrel
    re-exports, same directory, globally unique."""
    hit = [sid for sid, _m in files.get(fid, ())]
    if hit:
        return hit
    if qual and nm in COMMON_METHODS:
        return []
    # outside the caller's file, a bare call f() cannot reach a method
    for t in imports.get(fid, ()):
        if t in files:
            hit += _pick(files[t], qual)
    if not hit:
        for t in reexports.get(fid, ()):
            if t in files:
                hit += _pick(files[t], qual)
    fam = family_of(rpath)
    if not hit and fam in SAME_DIR_NS:
        rdir = rpath.rsplit("/", 1)[0] if "/" in rpath else ""
        hit = _pick(by_dir[nm].get(rdir, ()), qual)
    if not hit and not qual and fam in GLOBAL_NS and len(plain.get(nm, ())) == 1:
        hit = plain[nm][:]
    return hit


def _resolve_calls(conn, names: set[str] | None) -> None:
    """(Re)compute call edges for refs whose callee name is in `names` (None = all).

    Tiers, first hit wins: same file; files the caller imports; same directory (package-
    scoped languages); globally unique name (global-namespace languages). Bare calls f()
    never resolve to methods outside the caller's own file.
    """
    if names is not None and not names:
        return
    refs, syms = _load_call_inputs(conn, names)
    if not refs:
        return
    # name -> file_id -> [(symbol id, is_method)], plus a per-directory index and global counts
    by_file: dict[str, dict[int, list[tuple[int, bool]]]] = defaultdict(lambda: defaultdict(list))
    by_dir: dict[str, dict[str, list[tuple[int, bool]]]] = defaultdict(lambda: defaultdict(list))
    plain: dict[str, list[int]] = defaultdict(list)  # non-method definitions per name
    for sid, fid, nm, kind, spath in syms:
        is_m = kind == "method"
        by_file[nm][fid].append((sid, is_m))
        by_dir[nm][spath.rsplit("/", 1)[0] if "/" in spath else ""].append((sid, is_m))
        if not is_m:
            plain[nm].append(sid)
    imports, reexports = _import_maps(conn, refs, names)
    rows = []
    for ref_id, fid, caller, nm, line, qual, rpath in refs:
        files = by_file.get(nm)
        if not files:
            continue
        for sid in _callee_hits(fid, nm, qual, rpath, files, by_dir, plain, imports, reexports):
            if sid != caller:
                rows.append((caller, sid, "calls", line, ref_id))
    conn.executemany("INSERT INTO edges(src, dst, kind, line, ref_id) VALUES(?,?,?,?,?)", rows)


def _rebuild_import_edges(conn, file_ids: set[int] | None) -> None:
    sql = ("INSERT INTO edges(src, dst, kind, line) SELECT file_id, resolved_id, 'imports', "
           "MIN(line) FROM imports WHERE resolved_id IS NOT NULL {w} GROUP BY file_id, resolved_id")
    if file_ids is None:
        conn.execute("DELETE FROM edges WHERE kind = 'imports'")
        conn.execute(sql.format(w=""))
        return
    if not file_ids:
        return
    _temp_ids(conn, "imp_files", file_ids)
    conn.execute("DELETE FROM edges WHERE kind = 'imports' AND src IN (SELECT v FROM temp.imp_files)")
    conn.execute(sql.format(w="AND file_id IN (SELECT v FROM temp.imp_files)"))


@dataclass
class _Batch:
    """Rows collected while ingesting parsed files, inserted in bulk afterwards."""
    next_id: int
    next_ref: int
    sym_rows: list = field(default_factory=list)
    edge_rows: list = field(default_factory=list)
    ref_rows: list = field(default_factory=list)
    imp_rows: list = field(default_factory=list)


def _stat_files(root: Path, rels: list[str]) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for rel in rels:
        try:
            st = os.lstat(root / rel)
        except OSError:
            continue
        if stat_mod.S_ISREG(st.st_mode):
            stats[rel] = (st.st_mtime_ns, st.st_size)
    return stats


def _ingest(conn, r: dict, existing: dict, resolver: Resolver, names: set[str],
            import_dirty: set[int], batch: _Batch, res: UpdateResult) -> None:
    """Write one parsed file's node and queue its symbols, refs and imports."""
    rel, status = r["rel"], r["status"]
    if status == "error":
        return
    if status == "unchanged":
        res.unchanged += 1
        conn.execute("UPDATE nodes SET mtime_ns = ?, size = ? WHERE id = ?",
                     (r["mtime_ns"], r["size"], existing[rel][0]))
        return
    res.parsed += 1
    res.parsed_files.append(rel)
    res.parsers[r["parser"]] = res.parsers.get(r["parser"], 0) + 1
    file_cols = (r["lang"] or family_of(rel), r["parser"], r["loc"], r["size"],
                 r["mtime_ns"], r["hash"], r["branches"], r["nesting"], int(is_test_path(rel)))
    if rel in existing:
        fid = existing[rel][0]
        _purge_file(conn, fid, names, drop_file=False)
        conn.execute("UPDATE nodes SET lang=?, parser=?, loc=?, size=?, mtime_ns=?, "
                     "hash=?, branches=?, nesting=?, is_test=? WHERE id=?", (*file_cols, fid))
    else:
        res.added += 1
        fid = batch.next_id
        batch.next_id += 1
        conn.execute("INSERT INTO nodes(id, kind, name, path, lang, parser, loc, size, "
                     "mtime_ns, hash, branches, nesting, is_test, start_line, end_line) "
                     "VALUES(?, 'file', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)",
                     (fid, rel.rsplit("/", 1)[-1], rel, *file_cols))
    import_dirty.add(fid)
    ids = []
    for kind, nm, sl, el, parent, nb, nd in r["symbols"]:
        sid = batch.next_id
        batch.next_id += 1
        ids.append(sid)
        pid = ids[parent] if 0 <= parent < len(ids) - 1 else None
        batch.sym_rows.append((sid, kind, nm, rel, fid, pid, sl, el, nb, nd))
        batch.edge_rows.append((pid or fid, sid, "contains", sl, None))
        names.add(nm)
    for caller, nm, line, qual in r["refs"]:
        cid = ids[caller] if 0 <= caller < len(ids) else fid
        batch.ref_rows.append((batch.next_ref, fid, cid, nm, line, qual))
        batch.next_ref += 1
        names.add(nm)
    fam = family_of(rel)
    seen: set[tuple[str, int | None]] = set()
    for spec, line, optional in r["imports"]:
        targets = resolver.resolve(rel, fam, spec)
        if not targets and optional:
            continue
        for t in targets or [None]:
            if (spec, t) not in seen:
                seen.add((spec, t))
                batch.imp_rows.append((fid, spec, line, t))


def _relink_imports(conn, resolver: Resolver, path_ids: dict, import_dirty: set[int],
                    names: set[str]) -> None:
    rows = conn.execute(
        "SELECT i.rowid, i.file_id, i.spec, n.path FROM imports i JOIN nodes n "
        "ON n.id = i.file_id WHERE i.resolved_id IS NULL").fetchall()
    fixes, extra, fixed_files = [], [], set()
    for rowid, fid, spec, src in rows:
        if fid in import_dirty:  # just parsed and resolved above
            continue
        targets = [path_ids[t] for t in resolver.resolve(src, family_of(src), spec)
                   if t in path_ids]
        if targets:
            fixes.append((targets[0], rowid))
            extra += [(fid, spec, None, t) for t in targets[1:]]
            fixed_files.add(fid)
    conn.executemany("UPDATE imports SET resolved_id = ? WHERE rowid = ?", fixes)
    conn.executemany("INSERT INTO imports(file_id, spec, line, resolved_id) VALUES(?,?,?,?)",
                     extra)
    import_dirty |= fixed_files
    if fixed_files:
        _temp_ids(conn, "fix_files", fixed_files)
        names.update(nm for (nm,) in conn.execute(
            "SELECT DISTINCT name FROM refs WHERE file_id IN (SELECT v FROM temp.fix_files)"))


def _refresh_churn(conn, root: Path, gi, churn_days: int) -> None:
    data = ggit.churn(root, gi, churn_days)
    conn.execute("DELETE FROM churn")
    conn.executemany("INSERT INTO churn VALUES(?,?,?,?)",
                     [(p, c, a, t) for p, (c, a, t) in data.items()])
    gdb.set_meta(conn, "churn_commit", gi.head)
    gdb.set_meta(conn, "churn_days", str(churn_days))


# --------------------------------------------------------------------------- main
def update(root: str | Path, rebuild: bool = False, offline: bool | None = None,
           workers: int | None = None, churn_days: int = CHURN_DAYS) -> UpdateResult:
    """Incrementally bring the graph up to date. Returns counts incl. files parsed."""
    t0 = time.perf_counter()
    root = Path(root).resolve()
    if offline is None:
        offline = os.environ.get("CCR_OFFLINE", "") not in ("", "0")
    res = UpdateResult()
    path = gdb.db_path(root)
    if rebuild:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.remove(str(path) + suffix)
    full = not path.exists()
    conn = gdb.connect(path)
    if gdb.get_meta(conn, "schema_version") != gdb.SCHEMA_VERSION:
        conn.close()
        return update(root, rebuild=True, offline=offline, workers=workers, churn_days=churn_days)
    gdb.ensure_gitignored(root)
    res.full = full

    gi = ggit.info(root)
    listed = ggit.ls_files(root) if gi else None
    if listed is None:
        gi = None
        listed = walk_files(root)
    res.mode = "git" if gi else "fs"
    listed = [p for p in listed if not in_skipped_dir(p)]
    code_all = [p for p in listed if is_code(p)]
    aux_rels = [p for p in listed if p.rsplit("/", 1)[-1] in AUX_NAMES]

    existing = {r["path"]: (r["id"], r["mtime_ns"], r["size"], r["hash"]) for r in conn.execute(
        "SELECT id, path, mtime_ns, size, hash FROM nodes WHERE kind = 'file'")}
    stats = _stat_files(root, code_all)
    code = set(stats)
    res.scanned = len(code)

    git_changed: set[str] = set()
    last_commit = gdb.get_meta(conn, "commit")
    if gi and last_commit and not full:
        git_changed = ggit.changed_since(root, last_commit) or set()
    to_check: list[str] = []
    for rel in sorted(code):
        old = existing.get(rel)
        if old is None or (old[1], old[2]) != stats[rel] or rel in git_changed:
            to_check.append(rel)
    removed = [p for p in existing if p not in code]

    langs, allowed = _ts_languages(to_check, offline)
    jobs = [(str(root), rel, langs.get(rel), existing[rel][3] if rel in existing else None)
            for rel in to_check]
    results = _parse_all(jobs, allowed, workers)
    res.checked = len(results)

    failed = {r["rel"] for r in results if r["status"] == "error"}
    final_paths = code - failed
    resolver = Resolver(final_paths, _read_aux(root, aux_rels))

    names: set[str] = set()          # symbol names whose call edges must be recomputed
    import_dirty: set[int] = set()   # files whose import edges must be rebuilt
    with conn:
        for rel in removed + sorted(failed & set(existing)):
            _purge_file(conn, existing[rel][0], names, drop_file=True)
        res.removed = len(removed)
        next_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM nodes").fetchone()[0]) + 1
        next_ref = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM refs").fetchone()[0]) + 1
        batch = _Batch(next_id, next_ref)
        for r in results:
            _ingest(conn, r, existing, resolver, names, import_dirty, batch, res)
        sym_rows, edge_rows, ref_rows, imp_rows = (batch.sym_rows, batch.edge_rows,
                                                    batch.ref_rows, batch.imp_rows)
        # map resolved paths to ids (files inserted above are visible in this transaction)
        path_ids = {p: i for i, p in conn.execute("SELECT id, path FROM nodes WHERE kind='file'")}
        conn.executemany("INSERT INTO nodes(id, kind, name, path, file_id, parent_id, start_line, "
                         "end_line, branches, nesting) VALUES(?,?,?,?,?,?,?,?,?,?)", sym_rows)
        conn.executemany("INSERT INTO edges(src, dst, kind, line, ref_id) VALUES(?,?,?,?,?)",
                         edge_rows)
        conn.executemany("INSERT INTO refs(id, file_id, caller_id, name, line, qual) "
                         "VALUES(?,?,?,?,?,?)", ref_rows)
        conn.executemany("INSERT INTO imports(file_id, spec, line, resolved_id) VALUES(?,?,?,?)",
                         [(f, s, ln, path_ids.get(t) if t else None) for f, s, ln, t in imp_rows])

        # file set changed: previously unresolved imports elsewhere may resolve now
        if (res.added or res.removed) and not full:
            _relink_imports(conn, resolver, path_ids, import_dirty, names)

        _rebuild_import_edges(conn, None if full else import_dirty)
        _resolve_calls(conn, None if full else names)

        res.commit = gi.head if gi else None
        gdb.set_meta(conn, "commit", res.commit)
        gdb.set_meta(conn, "mode", res.mode)
        gdb.set_meta(conn, "built_at", str(int(time.time())))
        gdb.set_meta(conn, "root", str(root))
        if gi and (full or gdb.get_meta(conn, "churn_commit") != gi.head
                   or gdb.get_meta(conn, "churn_days") != str(churn_days)):
            _refresh_churn(conn, root, gi, churn_days)
            res.churn_updated = True
    conn.close()
    res.elapsed = time.perf_counter() - t0
    log.debug("graph update: %s", res)
    return res

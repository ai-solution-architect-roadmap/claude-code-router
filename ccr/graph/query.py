"""Read-only query API over .ccr/graph.db (sqlite only; never imports tree-sitter)."""
from __future__ import annotations

import os
import posixpath
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ccr.graph import db as gdb
from ccr.graph import git as ggit


@dataclass
class Symbol:
    id: int
    kind: str
    name: str
    path: str
    start_line: int
    end_line: int
    branches: int
    nesting: int
    parent: str | None = None
    callers: int = 0
    callees: int = 0

    @property
    def qualname(self) -> str:
        return f"{self.parent}.{self.name}" if self.parent else self.name


@dataclass
class Dependent:
    path: str
    depth: int
    via: str  # imports | calls


@dataclass
class TestLink:
    path: str
    reason: str   # imports | calls | name
    distance: int  # dependency depth for imports/calls; directory distance for name matches


_TEST_AFFIX_CI = re.compile(r"^(?:tests?_)|(?:[_\-](?:tests?|spec))$", re.I)
_TEST_AFFIX_CAMEL = re.compile(r"(?<=[a-z0-9])(?:Tests?|Spec)$|^Test(?=[A-Z_])")


def _subject(name: str, is_test: bool) -> str:
    """Normalized "what this file is about": test_foo.py / foo_test.go / FooTest.java -> foo."""
    stem = name.split(".", 1)[0]
    if is_test:
        prev = None
        while prev != stem:
            prev = stem
            stem = _TEST_AFFIX_CI.sub("", stem) or stem
            stem = _TEST_AFFIX_CAMEL.sub("", stem) or stem
    return re.sub(r"[_\-]", "", stem).lower()


def _dir_distance(a: str, b: str) -> int:
    pa, pb = posixpath.dirname(a).split("/"), posixpath.dirname(b).split("/")
    pa, pb = [p for p in pa if p], [p for p in pb if p]
    common = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        common += 1
    return (len(pa) - common) + (len(pb) - common)


class Graph:
    def __init__(self, conn: sqlite3.Connection, root: Path) -> None:
        self.conn = conn
        self.root = root

    @classmethod
    def open(cls, root: str | Path) -> "Graph | None":
        root = Path(root).resolve()
        p = gdb.db_path(root)
        if not p.exists():
            return None
        try:
            conn = gdb.connect(p, readonly=True)
            conn.execute("SELECT 1 FROM nodes LIMIT 1")
        except sqlite3.Error:
            return None
        return cls(conn, root)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------ basics
    def rel(self, path: str | Path) -> str:
        """Project-relative posix path for an absolute, cwd-relative or root-relative path."""
        p = Path(path)
        if not p.is_absolute() and not (self.root / p).exists() and p.exists():
            p = p.resolve()
        if p.is_absolute():
            try:
                p = p.resolve().relative_to(self.root)
            except ValueError:
                return p.as_posix()
        s = p.as_posix()
        while s.startswith("./"):
            s = s[2:]
        return s

    def file_row(self, path: str | Path) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM nodes WHERE kind = 'file' AND path = ?",
                                 (self.rel(path),)).fetchone()

    def find_files(self, fragment: str, limit: int = 20) -> list[str]:
        """Files whose path ends with (or contains) the fragment."""
        frag = fragment.strip("/")
        rows = self.conn.execute(
            "SELECT path FROM nodes WHERE kind = 'file' AND (path = ? OR path LIKE ? ESCAPE '\\') "
            "ORDER BY length(path) LIMIT ?", (frag, "%/" + _like(frag), limit)).fetchall()
        if not rows:
            rows = self.conn.execute(
                "SELECT path FROM nodes WHERE kind = 'file' AND path LIKE ? ESCAPE '\\' "
                "ORDER BY length(path) LIMIT ?", ("%" + _like(frag) + "%", limit)).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------ symbols
    def _symbols(self, where: str, args: tuple, limit: int) -> list[Symbol]:
        rows = self.conn.execute(
            "SELECT n.*, p.name AS parent_name, "
            "(SELECT COUNT(*) FROM edges e WHERE e.dst = n.id AND e.kind = 'calls') AS n_callers, "
            "(SELECT COUNT(*) FROM edges e WHERE e.src = n.id AND e.kind = 'calls') AS n_callees "
            "FROM nodes n LEFT JOIN nodes p ON p.id = n.parent_id "
            f"WHERE n.kind != 'file' AND {where} ORDER BY n.path, n.start_line LIMIT ?",
            (*args, limit)).fetchall()
        return [Symbol(r["id"], r["kind"], r["name"], r["path"], r["start_line"], r["end_line"],
                       r["branches"] or 0, r["nesting"] or 0, r["parent_name"], r["n_callers"],
                       r["n_callees"]) for r in rows]

    def lookup(self, query: str, limit: int = 20) -> list[Symbol]:
        """Find symbols: exact, case-insensitive, then prefix, then substring. Accepts A.b / A::b."""
        parts = [p for p in re.split(r"\.|::|#", query.strip()) if p]
        if not parts:
            return []
        name, parent = parts[-1], (parts[-2] if len(parts) > 1 else None)
        pclause, pargs = ("AND p.name = ?", (parent,)) if parent else ("", ())
        for where, arg in (("n.name = ?", name),
                           ("n.name = ? COLLATE NOCASE", name),
                           ("n.name LIKE ? ESCAPE '\\'", _like(name) + "%"),
                           ("n.name LIKE ? ESCAPE '\\'", "%" + _like(name) + "%")):
            found = self._symbols(f"{where} {pclause}", (arg, *pargs), limit)
            if found:
                return found
        return []

    def callers(self, symbol_id: int, limit: int = 50) -> list[tuple[str, str, int]]:
        """(caller qualname or file, path, line) for a symbol."""
        rows = self.conn.execute(
            "SELECT s.name, s.kind, s.path, e.line FROM edges e JOIN nodes s ON s.id = e.src "
            "WHERE e.dst = ? AND e.kind = 'calls' ORDER BY s.path, e.line LIMIT ?",
            (symbol_id, limit)).fetchall()
        return [(r[0] if r[1] != "file" else "<module>", r[2], r[3]) for r in rows]

    def callees(self, symbol_id: int, limit: int = 50) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT DISTINCT d.name, d.path FROM edges e JOIN nodes d ON d.id = e.dst "
            "WHERE e.src = ? AND e.kind = 'calls' LIMIT ?", (symbol_id, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]

    # ------------------------------------------------------------------ dependencies
    def dependencies(self, path: str | Path) -> list[str]:
        row = self.file_row(path)
        if row is None:
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT n.path FROM edges e JOIN nodes n ON n.id = e.dst "
            "WHERE e.src = ? AND e.kind = 'imports' ORDER BY n.path", (row["id"],))]

    def _direct_dependents(self, fid: int, calls: bool) -> list[tuple[int, str]]:
        out = [(r[0], "imports") for r in self.conn.execute(
            "SELECT DISTINCT src FROM edges WHERE dst = ? AND kind = 'imports'", (fid,))]
        if calls:
            out += [(r[0], "calls") for r in self.conn.execute(
                "SELECT DISTINCT COALESCE(s.file_id, s.id) FROM nodes d "
                "JOIN edges e ON e.dst = d.id AND e.kind = 'calls' JOIN nodes s ON s.id = e.src "
                "WHERE d.file_id = ?", (fid,))]
        return out

    def dependents(self, path: str | Path, depth: int = 3, calls: bool = True,
                   limit: int = 500) -> list[Dependent]:
        """Blast radius: files that (transitively) import or call into `path`."""
        row = self.file_row(path)
        if row is None:
            return []
        start = row["id"]
        seen = {start: (0, "")}
        q = deque([start])
        while q and len(seen) <= limit:
            cur = q.popleft()
            d = seen[cur][0]
            if d >= depth:
                continue
            for nid, via in self._direct_dependents(cur, calls):
                if nid not in seen:
                    seen[nid] = (d + 1, via)
                    q.append(nid)
        ids = [i for i in seen if i != start]
        if not ids:
            return []
        paths = dict(self.conn.execute(
            f"SELECT id, path FROM nodes WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall())
        out = [Dependent(paths[i], seen[i][0], seen[i][1]) for i in ids if i in paths]
        return sorted(out, key=lambda x: (x.depth, x.path))

    # ------------------------------------------------------------------ metrics
    def complexity(self, path: str | Path) -> dict | None:
        row = self.file_row(path)
        if row is None:
            return None
        syms = self.conn.execute(
            "SELECT name, kind, start_line, end_line, branches, nesting FROM nodes "
            "WHERE file_id = ? ORDER BY branches DESC, nesting DESC", (row["id"],)).fetchall()
        funcs = [s for s in syms if s["kind"] in ("function", "method", "macro")]
        return {
            "path": row["path"], "lang": row["lang"], "parser": row["parser"],
            "loc": row["loc"] or 0, "size": row["size"] or 0,
            "branches": row["branches"] or 0, "nesting": row["nesting"] or 0,
            "symbols": len(syms),
            "max_function_branches": funcs[0]["branches"] if funcs else 0,
            "max_function_lines": max(((s["end_line"] or s["start_line"]) - s["start_line"] + 1
                                       for s in funcs), default=0),
            "hotspots": [dict(name=s["name"], kind=s["kind"], line=s["start_line"],
                              lines=(s["end_line"] or s["start_line"]) - s["start_line"] + 1,
                              branches=s["branches"], nesting=s["nesting"])
                         for s in funcs[:5] if s["branches"]],
        }

    def churn(self, path: str | Path) -> dict | None:
        r = self.conn.execute("SELECT commits, authors, last_ts FROM churn WHERE path = ?",
                              (self.rel(path),)).fetchone()
        if r is None:
            return None
        return {"commits": r[0], "authors": r[1], "last_ts": r[2],
                "window_days": int(gdb.get_meta(self.conn, "churn_days") or 0)}

    def tests_for(self, path: str | Path) -> list[TestLink]:
        """Test files near `path`: tests that import/call it, then name-matched tests."""
        rel = self.rel(path)
        row = self.file_row(rel)
        links: dict[str, TestLink] = {}
        if row is not None:
            for dep in self.dependents(rel, depth=2):
                frow = self.file_row(dep.path)
                if frow is not None and frow["is_test"]:
                    links[dep.path] = TestLink(dep.path, dep.via, dep.depth)
        subject = _subject(posixpath.basename(rel), False)
        if subject:
            for (tp,) in self.conn.execute("SELECT path FROM nodes WHERE kind='file' AND is_test=1"):
                if tp in links or tp == rel:
                    continue
                if _subject(posixpath.basename(tp), True) == subject:
                    links[tp] = TestLink(tp, "name", _dir_distance(rel, tp))
        order = {"imports": 0, "calls": 1, "name": 2}
        return sorted(links.values(), key=lambda t: (order[t.reason], t.distance, t.path))

    def test_proximity(self, path: str | Path) -> dict:
        tests = self.tests_for(path)
        frow = self.file_row(path)
        return {"is_test": bool(frow and frow["is_test"]), "has_tests": bool(tests),
                "nearest": tests[0].path if tests else None,
                "direct": any(t.reason != "name" and t.distance == 1 for t in tests),
                "tests": [t.path for t in tests[:10]]}

    # ------------------------------------------------------------------ overview
    def stats(self) -> dict:
        c = self.conn
        kinds = dict(c.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall())
        files = kinds.pop("file", 0)
        meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
        return {
            "files": files,
            "symbols": sum(kinds.values()),
            "symbol_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "edges": dict(c.execute("SELECT kind, COUNT(*) FROM edges GROUP BY kind").fetchall()),
            "languages": dict(c.execute("SELECT lang, COUNT(*) FROM nodes WHERE kind='file' "
                                        "GROUP BY lang ORDER BY 2 DESC").fetchall()),
            "parsers": dict(c.execute("SELECT parser, COUNT(*) FROM nodes WHERE kind='file' "
                                      "GROUP BY parser ORDER BY 2 DESC").fetchall()),
            "tests": c.execute("SELECT COUNT(*) FROM nodes WHERE kind='file' AND is_test=1")
                      .fetchone()[0],
            "unresolved_imports": c.execute("SELECT COUNT(*) FROM imports WHERE resolved_id IS NULL")
                                   .fetchone()[0],
            "churn_files": c.execute("SELECT COUNT(*) FROM churn").fetchone()[0],
            "meta": meta,
            "db_bytes": _db_bytes(gdb.db_path(self.root)),
        }

    def hot_files(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT path, commits, authors FROM churn c WHERE EXISTS "
            "(SELECT 1 FROM nodes n WHERE n.kind='file' AND n.path = c.path) "
            "ORDER BY commits DESC LIMIT ?", (limit,)).fetchall()
        return [dict(path=r[0], commits=r[1], authors=r[2]) for r in rows]


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _db_bytes(p: Path) -> int:
    total = 0
    for suffix in ("", "-wal"):
        try:
            total += os.path.getsize(str(p) + suffix)
        except OSError:
            pass
    return total


def freshness(root: str | Path, check_files: bool = True) -> dict:
    """status: missing | fresh | stale | error, plus reasons. Cheap enough for `ccr doctor`."""
    root = Path(root).resolve()
    g = Graph.open(root)
    if g is None:
        return {"status": "missing" if not gdb.db_path(root).exists() else "error",
                "reasons": []}
    with g:
        meta = dict(g.conn.execute("SELECT key, value FROM meta").fetchall())
        files = g.conn.execute("SELECT path, mtime_ns, size FROM nodes WHERE kind='file'").fetchall()
    out = {"status": "fresh", "reasons": [], "files": len(files), "mode": meta.get("mode"),
           "commit": meta.get("commit"), "built_at": int(meta.get("built_at") or 0)}
    out["age_s"] = int(time.time()) - out["built_at"] if out["built_at"] else None
    if meta.get("mode") == "git":
        gi = ggit.info(root)
        if gi and gi.head and gi.head != meta.get("commit"):
            out["reasons"].append("HEAD moved since last index")
    if check_files:
        changed = 0
        for path, mtime, size in files:
            try:
                st = os.stat(root / path)
            except OSError:
                changed += 1
                continue
            if (st.st_mtime_ns, st.st_size) != (mtime, size):
                changed += 1
        if changed:
            out["reasons"].append(f"{changed} indexed file(s) modified or deleted")
    if out["reasons"]:
        out["status"] = "stale"
    return out

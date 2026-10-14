"""Locate the code a task touches: @-mentions and named paths, symbol names, path words and
memory entities, matched against the code graph (or the file tree when no graph exists).

Every candidate carries a confidence in [0, 1] and the reasons it was picked. Evidence for the
same file is combined noisy-or style, so two weak hits ("stripe" + "client") reinforce.
"""
from __future__ import annotations

import math
import posixpath
from dataclasses import dataclass, field
from pathlib import Path

from ccr.evaluate import text as T

BROAD_WORD_FILES = 25     # a word matching more files than this is too broad to use
MAX_CANDIDATES = 40
MAX_OTHER_FILES = 5000    # non-graph files (docs, config, IaC) considered for matching
TOOL_CONFIG_PREFIXES = (".claude/", ".ccr/")
_CODE_EXT_PARTS = {"py", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "go", "rs", "java", "kt",
                   "rb", "php", "cs", "c", "h", "cc", "cpp", "hpp", "swift", "scala", "zig", "md",
                   "json", "yaml", "yml", "toml", "txt", "lua", "ex", "exs", "dart", "sh"}
# path components that say nothing about the area
_GENERIC_DIRS = {"src", "lib", "app", "apps", "pkg", "internal", "cmd", "main", "java", "com",
                 "org", "index", "mod", "init", "__init__", "core", "common", "shared", "utils",
                 "util", "helpers", "test", "tests", "spec", "__tests__"}


@dataclass
class Candidate:
    path: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    indexed: bool = True     # present in the code graph
    is_test: bool = False


@dataclass
class Location:
    candidates: list[Candidate] = field(default_factory=list)
    explicit: list[str] = field(default_factory=list)      # resolved @-mentions / named paths
    mentioned: list[str] = field(default_factory=list)     # @-mentions (subset of explicit)
    unresolved: list[str] = field(default_factory=list)    # named paths not found
    symbols: list[str] = field(default_factory=list)       # identifiers that resolved
    terms: list[str] = field(default_factory=list)         # plain words that matched paths
    broad_terms: list[str] = field(default_factory=list)
    memory_terms: list[str] = field(default_factory=list)
    confidence: float = 0.0
    graph: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def located(self) -> bool:
        return self.confidence >= 0.45

    def primary(self, limit: int = 8) -> list[Candidate]:
        """The files risk is scored over."""
        strong = [c for c in self.candidates if c.confidence >= 0.4]
        if strong:
            return strong[:limit]
        return [c for c in self.candidates if c.confidence >= 0.2][:3]

    def top_area(self) -> str:
        """Coarse area key for history: up to two leading directories of the top candidate."""
        prim = self.primary(1)
        if not prim:
            return "*"
        d = posixpath.dirname(prim[0].path)
        return "/".join(d.split("/")[:2]) if d else "."


class _Acc:
    def __init__(self):
        self.miss: dict[str, float] = {}
        self.reasons: dict[str, list[str]] = {}

    def add(self, path: str, conf: float, reason: str) -> None:
        conf = max(0.0, min(1.0, conf))
        self.miss[path] = self.miss.get(path, 1.0) * (1.0 - conf)
        rs = self.reasons.setdefault(path, [])
        if reason not in rs:
            rs.append(reason)

    def items(self):
        for p, m in self.miss.items():
            yield p, 1.0 - m, self.reasons[p]


def _universe(root: Path, graph) -> tuple[list[str], set[str], set[str]]:
    """(all known files, graph-indexed files, test files)."""
    indexed: set[str] = set()
    tests: set[str] = set()
    if graph is not None:
        for path, is_test in graph.conn.execute(
                "SELECT path, is_test FROM nodes WHERE kind = 'file'"):
            indexed.add(path)
            if is_test:
                tests.add(path)
    others: list[str] = []
    try:
        from ccr.graph import git as ggit
        listed = ggit.ls_files(root)   # None outside git
    except Exception:
        listed = None
    if listed is None:
        try:
            from ccr.graph.files import walk_files
            listed = walk_files(root)
        except Exception:
            listed = []
    from ccr.graph.files import in_skipped_dir, is_skipped_name, is_test_path
    for p in listed:
        if p in indexed or in_skipped_dir(p) or is_skipped_name(posixpath.basename(p)):
            continue
        # Claude Code's own config (agents, settings, commands) is not project code: its
        # words ("explore", "implement", "tests") would match ordinary prompts. Named paths
        # still resolve through the is_file() fallback in locate().
        if p.startswith(TOOL_CONFIG_PREFIXES):
            continue
        others.append(p)
    if len(others) > MAX_OTHER_FILES:   # huge trees: keep the shallowest non-code files
        others = sorted(others, key=lambda x: (x.count("/"), x))[:MAX_OTHER_FILES]
    if graph is None:
        tests.update(p for p in others if is_test_path(p))
    return sorted(indexed) + sorted(others), indexed, tests


def _components(path: str) -> tuple[list[str], list[str]]:
    """(directory word components, file-stem word components)."""
    parts = path.split("/")
    dirs: list[str] = []
    for d in parts[:-1]:
        dirs += T.split_ident(d)
    name = parts[-1]
    stem = name.split(".")[0] if not name.startswith(".") else name[1:]
    return dirs, [c for c in T.split_ident(stem) if c not in _CODE_EXT_PARTS]


def _sym_rows(graph, name: str, nocase: bool) -> list[tuple[str, str | None, str]]:
    op = "= ? COLLATE NOCASE" if nocase else "= ?"
    return [(r[0], r[1], r[2]) for r in graph.conn.execute(
        "SELECT DISTINCT n.path, p.name, n.name FROM nodes n LEFT JOIN nodes p "
        f"ON p.id = n.parent_id WHERE n.kind != 'file' AND n.name {op} LIMIT 60", (name,))]


@dataclass
class _Ctx:
    """State shared by the matching stages of `locate`."""
    task: str
    root: Path
    graph: object
    loc: Location
    acc: _Acc
    universe: list[str]
    indexed: set[str]
    tests: set[str]
    wants_tests: bool
    uni_set: set[str] = field(init=False)
    _suffix: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.uni_set = set(self.universe)

    def suffix(self, frag: str) -> list[str]:
        if frag not in self._suffix:
            f = frag.strip("/")
            self._suffix[frag] = [p for p in self.universe if p == f or p.endswith("/" + f)]
        return self._suffix[frag]


def _match_paths(c: _Ctx) -> None:
    """1) @-mentions and named paths."""
    loc = c.loc
    ats, named = T.path_tokens(c.task)
    for tok, how in [(a, "@-mentioned") for a in ats] + [(n, "named in prompt") for n in named]:
        frag = tok.strip("/")
        if frag in c.uni_set:
            hits = [frag]
        else:
            under = [p for p in c.universe if p.startswith(frag + "/")]
            if under:
                n = len(under)
                for p in under[:30]:
                    c.acc.add(p, max(0.35, 0.9 / math.sqrt(n)), f"{how} (in {frag}/)")
                loc.explicit.append(frag + "/")
                if how == "@-mentioned":
                    loc.mentioned.append(frag + "/")
                continue
            hits = c.suffix(frag)
            if not hits and (c.root / frag).is_file():
                hits = [frag]
                c.universe.append(frag)
                c.uni_set.add(frag)
        if not hits:
            loc.unresolved.append(tok)
            continue
        conf = 1.0 if len(hits) == 1 else 0.9 / math.sqrt(len(hits))
        for p in hits[:10]:
            c.acc.add(p, conf, how)
        loc.explicit.append(hits[0] if len(hits) == 1 else frag)
        if how == "@-mentioned":
            loc.mentioned.append(hits[0] if len(hits) == 1 else frag)


def _symbol_files(graph, name: str, qual: str | None) -> tuple[list[str], bool]:
    """Files defining `name` (narrowed by a qualifier), and whether the match was exact-case."""
    rows = _sym_rows(graph, name, nocase=False)
    exact = bool(rows)
    if not rows:
        rows = _sym_rows(graph, name, nocase=True)
    if qual:
        q = qual.lower()
        narrowed = [r for r in rows if (r[1] or "").lower() == q
                    or q in r[0].lower().replace("_", "")]
        rows = narrowed or rows
    return sorted({r[0] for r in rows}), exact


def _match_identifiers(c: _Ctx) -> set[str]:
    """2) identifiers -> symbols (graph) or file stems. Returns the words they consumed."""
    loc = c.loc
    used_words: set[str] = set()
    for ident in T.identifiers(c.task):
        parts = [p for p in ident.replace("::", ".").replace("#", ".").split(".") if p]
        name, qual = parts[-1], (parts[-2] if len(parts) > 1 else None)
        found: list[str] = []
        exact = False
        if c.graph is not None:
            found, exact = _symbol_files(c.graph, name, qual)
        stem_hits = [p for p in c.universe
                     if posixpath.basename(p).split(".")[0].lower() in
                     {name.lower(), (qual or "").lower()} - {""}]
        if found:
            base = 0.85 if exact else 0.6
            for p in found[:20]:
                c.acc.add(p, base / math.sqrt(len(found)), f"symbol {ident}")
            loc.symbols.append(ident)
        if stem_hits and len(stem_hits) <= 10:
            for p in stem_hits:
                c.acc.add(p, 0.75 / math.sqrt(len(stem_hits)), f"file name ~ {ident}")
            if ident not in loc.symbols:
                loc.symbols.append(ident)
        if found or stem_hits:  # resolved: its words are not re-used as plain path words
            used_words.update(w.lower() for w in T.split_ident(ident))
            used_words.update(p.lower() for p in parts)
    return used_words


def _match_words(c: _Ctx, used_words: set[str]) -> None:
    """3) plain words -> symbol names and path components."""
    loc = c.loc
    comps = {p: _components(p) for p in c.universe}
    for w in T.content_words(c.task):
        if w in used_words or len(w) < 3:
            continue
        sw = T.stem(w)
        file_hits = [p for p, (_, fs) in comps.items() if any(T.stem(x) == sw for x in fs)]
        dir_hits = [p for p, (ds, fs) in comps.items() if p not in file_hits
                    and any(T.stem(x) == sw for x in ds) and sw not in _GENERIC_DIRS]
        sym_hits: list[str] = []
        if c.graph is not None and len(w) >= 4:
            sym_hits = sorted({r[0] for r in _sym_rows(c.graph, w, nocase=True)})
        hit_set = set(file_hits) | set(dir_hits) | set(sym_hits)
        if not hit_set:
            continue
        # tests that merely share the word do not dilute the match for the code they test
        non_test = hit_set - c.tests if not c.wants_tests else hit_set
        total = len(non_test) or len(hit_set)
        if total > BROAD_WORD_FILES:
            loc.broad_terms.append(w)
            continue
        loc.terms.append(w)
        n = math.sqrt(total)
        for p in file_hits:
            c.acc.add(p, 0.5 / n, f"path word '{w}'")
        for p in dir_hits:
            c.acc.add(p, 0.35 / n, f"directory '{w}'")
        for p in sym_hits:
            c.acc.add(p, 0.45 / n, f"symbol name '{w}'")


def _match_memory(c: _Ctx, memory) -> None:
    """4) memory entities mentioned in the prompt."""
    if memory is None or not getattr(memory, "signals", None):
        return
    from ccr.memory import matches
    loc = c.loc
    prompt_words = T.words(T.strip_refs(c.task))
    seen_terms: set[str] = set()
    for sig in memory.signals:
        term = sig.path_or_term
        key = term.lower()
        if key in seen_terms:
            continue
        significant = [x for x in T.words(term) if x not in T.STOP and len(x) >= 3
                       and x not in _CODE_EXT_PARTS]
        if not significant:
            continue
        mentioned = any(term == e or term.strip("/") == e.strip("/") for e in loc.explicit) \
            or T.phrase_in(" ".join(significant), prompt_words)
        if not mentioned:
            continue
        seen_terms.add(key)
        hits = [p for p in c.universe if matches(sig, p)]
        if not hits or len(hits) > BROAD_WORD_FILES:
            continue
        loc.memory_terms.append(term)
        for p in hits:
            c.acc.add(p, 0.55 / math.sqrt(len(hits)), f"memory entity '{term}'")


def _rank(c: _Ctx) -> list[Candidate]:
    cands = []
    for p, conf, reasons in c.acc.items():
        is_test = p in c.tests
        if is_test and not c.wants_tests and not any(r in ("@-mentioned", "named in prompt")
                                                     for r in reasons):
            conf *= 0.6
        cands.append(Candidate(p, round(conf, 3), reasons, p in c.indexed, is_test))
    cands.sort(key=lambda x: (-x.confidence, len(x.path), x.path))
    return cands


def locate(task: str, root: str | Path, graph=None, memory=None,
           files: list[str] | None = None) -> Location:
    """Find candidate files for `task`. `graph`: open ccr.graph.Graph or None; `memory`:
    ccr.memory.MemoryReport or None; `files`: override the file universe (tests)."""
    root = Path(root).resolve()
    loc = Location(graph=graph is not None)
    if files is not None:
        from ccr.graph.files import is_test_path
        universe = sorted(set(files))
        indexed, tests = set(), {p for p in universe if is_test_path(p)}
    else:
        universe, indexed, tests = _universe(root, graph)
    if graph is None:
        loc.notes.append("code graph not built (run `ccr graph`): matching paths only, "
                         "no blast radius / complexity")
    c = _Ctx(task=task, root=root, graph=graph, loc=loc, acc=_Acc(), universe=universe,
             indexed=indexed, tests=tests,
             wants_tests=any(T.stem(w) == "test" for w in T.words(task)))
    _match_paths(c)
    used_words = _match_identifiers(c)
    _match_words(c, used_words)
    _match_memory(c, memory)

    cands = _rank(c)
    loc.candidates = cands[:MAX_CANDIDATES]
    if loc.explicit and not loc.unresolved:
        loc.confidence = 1.0
    elif loc.explicit:
        loc.confidence = 0.7
    elif cands:
        top = cands[0].confidence
        crowd = sum(1 for x in cands if x.confidence >= 0.8 * top)
        loc.confidence = round(top * (0.85 if crowd > 3 else 1.0), 3)
    if loc.unresolved:
        loc.notes.append("named but not found: " + ", ".join(loc.unresolved))
    return loc

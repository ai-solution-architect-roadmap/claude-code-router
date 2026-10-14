"""Code risk over the located candidate set.

Components (each in [0, 1], reported with a human-readable detail):
  blast_radius    transitive dependents (imports + calls, depth 3)
  complexity      worst function branches, nesting depth, file size
  test_proximity  1 = no tests found, 0.4 = only name-matched/indirect tests, 0 = direct tests
  churn           commits / authors in the git churn window
  critical_path   configurable critical globs (auth, payments, migrations, infra, ...) or
                  critical words in the prompt
  memory          risk flags from CLAUDE.md / auto memory / agent memory / MCP memory

Each file's values are scaled by how sure we are it is touched, then the worst file wins per
component. The overall score is a weighted noisy-or, so one strong factor is enough for
"high" and several moderate ones add up.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from functools import lru_cache

from ccr.evaluate import text as T

COMPONENTS = ("blast_radius", "complexity", "test_proximity", "churn", "critical_path", "memory")
CODE_COMPONENTS = ("blast_radius", "complexity", "test_proximity", "churn")
LABELS = {"blast_radius": "blast radius", "complexity": "complexity",
          "test_proximity": "test proximity", "churn": "churn",
          "critical_path": "critical path", "memory": "memory flags"}
MEMORY_KIND_VALUE = {"protected": 1.0, "critical": 0.9, "fragile": 0.6, "hard": 0.5,
                     "ownership": 0.3}
PROMPT_CRITICAL_VALUE = 0.6
CRITICAL_MIN_CONF = 0.45   # a weakly-located file (one directory word) is not a critical hit


@dataclass
class Component:
    name: str
    value: float = 0.0
    detail: str = "none"
    files: list[str] = field(default_factory=list)


@dataclass
class MemoryFlag:
    kind: str
    term: str
    path: str
    text: str
    source: str


@dataclass
class Risk:
    score: float | None = None
    level: str = "unknown"                 # low | medium | high | unknown
    code_risk: float = 0.0                 # noisy-or of the code-only components
    components: dict[str, Component] = field(default_factory=dict)
    critical_groups: list[str] = field(default_factory=list)
    critical_files: list[str] = field(default_factory=list)
    critical_terms: list[str] = field(default_factory=list)
    memory_flags: list[MemoryFlag] = field(default_factory=list)
    evaluated: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    dependents: int | None = None          # files depending on the located set (graph only)
    tests: str | None = None               # direct | name-matched | none (graph only)

    @property
    def critical(self) -> bool:
        return bool(self.critical_files or self.critical_terms)

    @property
    def critical_located(self) -> bool:
        return bool(self.critical_files)

    def has_memory(self, *kinds: str) -> bool:
        return any(f.kind in kinds for f in self.memory_flags)


@lru_cache(maxsize=512)
def glob_rx(pattern: str) -> re.Pattern:
    """gitignore-ish glob: `**` spans dirs, `*`/`?` do not; no `/` means "any directory"."""
    pat = pattern.strip().lower()
    if pat.startswith("/"):
        pat = pat[1:]
    elif not pat.startswith("**/"):
        pat = "**/" + pat
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out += "(?:/.*)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif pat[i] == "*":
            out += "[^/]*"
            i += 1
        elif pat[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pat[i])
            i += 1
    return re.compile(out + r"\Z")


def critical_groups_for(path: str, groups: dict[str, list[str]]) -> list[str]:
    p = path.lower()
    return [g for g, pats in groups.items() if any(glob_rx(x).match(p) for x in pats)]


def prompt_critical_terms(task: str, groups: dict[str, list[str]]) -> list[tuple[str, str]]:
    """[(group, term)] for critical words in the prompt (paths/backticks excluded)."""
    ws = T.words(T.strip_refs(task))
    out = []
    for g, terms in groups.items():
        for t in terms:
            if T.phrase_in(t, ws):
                out.append((g, t))
    return out


def noisy_or(values: dict[str, float], weights: dict[str, float], names) -> float:
    miss = 1.0
    for n in names:
        miss *= 1.0 - weights.get(n, 0.0) * max(0.0, min(1.0, values.get(n, 0.0)))
    return round(1.0 - miss, 3)


class _Best:
    """Strongest evidence per risk component: (value, detail, file)."""

    def __init__(self) -> None:
        self.items = {n: (0.0, "", None) for n in COMPONENTS}

    def offer(self, name: str, value: float, detail: str, path: str | None) -> None:
        if value > self.items[name][0] or (self.items[name][2] is None and detail):
            self.items[name] = (value, detail, path)


def _scale(conf: float) -> float:
    return min(1.0, conf / 0.7)


_TEST_RANK = {"none": 0, "name-matched": 1, "direct": 2}


def _file_metrics(risk: Risk, c, graph, rc: dict, full_blast: float, best: _Best,
                  blast_union: set[str]) -> None:
    """Offer blast radius, complexity, test proximity and churn evidence for one candidate."""
    s = _scale(c.confidence)
    try:
        deps = graph.dependents(c.path)
    except Exception:
        deps = []
    blast_union.update(d.path for d in deps)
    n = len(deps)
    direct = sum(1 for d in deps if d.depth == 1)
    v = min(1.0, math.log2(1 + n) / full_blast) if n else 0.0
    best.offer("blast_radius", v * s, f"{n} dependents of {c.path} ({direct} direct)"
               if n else f"no dependents of {c.path}", c.path)
    cx = graph.complexity(c.path) or {}
    mfb, nest, loc = (cx.get("max_function_branches", 0), cx.get("nesting", 0), cx.get("loc", 0))
    v = (0.5 * min(1.0, mfb / rc["complexity_full_branches"])
         + 0.25 * min(1.0, nest / 5) + 0.25 * min(1.0, loc / 600))
    hot = (cx.get("hotspots") or [{}])[0].get("name")
    best.offer("complexity", v * s, f"{c.path}: max function branches {mfb}"
               + (f" ({hot})" if hot else "") + f", nesting {nest}, {loc} loc", c.path)
    if not c.is_test:
        _test_metrics(risk, c, graph, s, best)
    ch = graph.churn(c.path)
    if ch:
        v = (0.7 * min(1.0, ch["commits"] / rc["churn_full_commits"])
             + 0.3 * min(1.0, ch["authors"] / 5))
        best.offer("churn", v * s, f"{c.path}: {ch['commits']} commits, {ch['authors']} "
                   f"author(s) in {ch['window_days']}d", c.path)


def _test_metrics(risk: Risk, c, graph, s: float, best: _Best) -> None:
    tests = graph.tests_for(c.path)
    linked = [t for t in tests if t.reason != "name"]
    kind = "direct" if linked else "name-matched" if tests else "none"
    if risk.tests is None or _TEST_RANK[kind] > _TEST_RANK[risk.tests]:
        risk.tests = kind
    if not tests:
        best.offer("test_proximity", 1.0 * s, f"no tests found for {c.path}", c.path)
    elif not linked:
        best.offer("test_proximity", 0.4 * s,
                   f"{c.path}: only name-matched tests ({tests[0].path})", c.path)
    else:
        best.offer("test_proximity", 0.0, f"{c.path} covered by {linked[0].path} "
                   f"({linked[0].reason})", c.path)


def _code_metrics(risk: Risk, prim, graph, rc: dict, best: _Best) -> None:
    if graph is None:
        if prim:
            risk.notes.append("no code graph: blast radius, complexity, tests and churn unknown")
        return
    blast_union: set[str] = set()
    full_blast = math.log2(1 + rc["blast_full"])
    for c in prim:
        if c.indexed:
            _file_metrics(risk, c, graph, rc, full_blast, best, blast_union)
    if prim:
        blast_union -= {c.path for c in prim}
        risk.dependents = len(blast_union)
        if len(prim) > 1 and blast_union:
            v, d, p = best.items["blast_radius"]
            best.items["blast_radius"] = (v, d + f"; {len(blast_union)} across all candidates", p)


def _critical_paths(risk: Risk, prim, task: str, cfg, best: _Best) -> None:
    groups = cfg["critical_paths"]
    for c in prim:
        hit = critical_groups_for(c.path, groups)
        if hit and c.confidence >= CRITICAL_MIN_CONF:
            risk.critical_files.append(c.path)
            for g in hit:
                if g not in risk.critical_groups:
                    risk.critical_groups.append(g)
            best.offer("critical_path", _scale(c.confidence), f"{'/'.join(hit)}: {c.path}", c.path)
    for g, t in prompt_critical_terms(task, cfg["critical_terms"]):
        risk.critical_terms.append(t)
        if g not in risk.critical_groups:
            risk.critical_groups.append(g)
    current = best.items["critical_path"]
    if risk.critical_terms and current[0] < PROMPT_CRITICAL_VALUE:
        terms = ", ".join(f"'{t}'" for t in risk.critical_terms[:3])
        best.items["critical_path"] = (
            PROMPT_CRITICAL_VALUE,
            f"prompt mentions {terms} ({'/'.join(risk.critical_groups)})", None)
    elif risk.critical_terms:
        v, d, p = current
        best.items["critical_path"] = (v, d + f"; prompt mentions '{risk.critical_terms[0]}'", p)


def _memory_flags(risk: Risk, prim, memory, best: _Best) -> None:
    if memory is None:
        return
    for c in prim:
        for sig in memory.signals_for(c.path):
            risk.memory_flags.append(MemoryFlag(sig.kind, sig.path_or_term, c.path,
                                                sig.text, sig.source))
            v = MEMORY_KIND_VALUE.get(sig.kind, 0.3) * _scale(c.confidence)
            best.offer("memory", v, f"{sig.kind}: '{sig.path_or_term}' ({_short(sig.text)})", c.path)


def assess_risk(location, task: str, cfg, graph=None, memory=None) -> Risk:
    rc = cfg["risk"]
    weights = rc["weights"]
    risk = Risk(components={n: Component(n) for n in COMPONENTS})
    comp = risk.components
    prim = location.primary()
    risk.evaluated = [c.path for c in prim]
    best = _Best()

    _code_metrics(risk, prim, graph, rc, best)
    _critical_paths(risk, prim, task, cfg, best)
    _memory_flags(risk, prim, memory, best)

    for n in COMPONENTS:
        v, d, p = best.items[n]
        comp[n].value = round(v, 3)
        if d:
            comp[n].detail = d
        comp[n].files = [p] if p else []
    vals = {n: comp[n].value for n in COMPONENTS}
    risk.code_risk = noisy_or(vals, weights, CODE_COMPONENTS)
    if not prim and not risk.critical_terms:
        risk.notes.append("no files located: risk unknown")
        return risk
    risk.score = noisy_or(vals, weights, COMPONENTS)
    risk.level = ("high" if risk.score >= rc["high"] else
                  "medium" if risk.score >= rc["medium"] else "low")
    return risk


def _short(s: str, n: int = 70) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."

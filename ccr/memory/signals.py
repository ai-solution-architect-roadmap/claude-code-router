"""Keyword heuristics that turn memory text into risk/ownership signals."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MemorySignal:
    path_or_term: str
    kind: str     # fragile | critical | protected | hard | ownership
    text: str
    source: str   # file the signal came from (path), or "mcp:<server>"


KIND_RX: dict[str, re.Pattern] = {
    "protected": re.compile(
        r"\b(?:do\s+not|don'?t|never|must\s+not|should\s+not)\s+(?:ever\s+)?(?:edit|modify|touch|"
        r"change|delete|rename|regenerate|hand[- ]edit)\b|\bauto-?generated\b|\bread[- ]only\b|"
        r"\bfrozen\b", re.I),
    "fragile": re.compile(
        r"\b(?:fragile|brittle|flaky|delicate|finicky|tricky|subtle|easy\s+to\s+break|"
        r"breaks\s+easily|be\s+careful|careful\s+(?:with|when|around|about)|handle\s+with\s+care|"
        r"gotchas?|footguns?|pitfalls?|race\s+conditions?|regress(?:ion|ions|es)?|"
        r"watch\s+out)\b", re.I),
    "critical": re.compile(
        r"\b(?:critical|mission[- ]critical|load[- ]bearing|high[- ]risk|risky|dangerous|"
        r"security[- ](?:sensitive|critical)|sensitive|hot\s+path|blast\s+radius|"
        r"production[- ]critical)\b", re.I),
    "hard": re.compile(
        r"\b(?:complex|complicated|hard\s+to|difficult|confusing|convoluted|legacy|spaghetti|"
        r"gnarly|hairy|messy|tech(?:nical)?\s+debt|poorly\s+understood|nobody\s+understands|"
        r"known[- ]hard|hardest)\b", re.I),
    "ownership": re.compile(
        r"\b(?:owned\s+by|owners?\s*[:=]|owns\b|maintained\s+by|maintainers?\s*[:=]|"
        r"ask\s+@?[\w.-]+\s+before|ping\s+@[\w.-]+|code\s*owners?|point\s+of\s+contact|"
        r"(?:is|are)\s+responsible\s+for|reviewed\s+by\s+@)", re.I),
}

_CODE_EXT = (r"py|pyi|ts|tsx|js|jsx|mjs|go|rs|java|kt|scala|rb|php|cs|c|h|cc|cpp|hpp|swift|"
             r"sql|ya?ml|json|toml|ini|cfg|md|sh|zig|ex|exs|lua|dart|vue|svelte|proto|tf")
_PATH_RX = re.compile(
    r"(?<![\w@/.-])((?:\.{0,2}/)?(?:[\w.*-]+/)+[\w.*-]*|[\w*-]+(?:\.[\w-]+)*\.(?:" + _CODE_EXT +
    r"))(?![\w/])")
_TICK_RX = re.compile(r"`([^`\n]{1,120})`")
_PHRASE_RX = re.compile(
    r"\b(?:careful\s+(?:with|when\s+(?:touching|changing|editing|modifying)|around|about)|"
    r"(?:don'?t|do\s+not|never)\s+(?:touch|edit|modify|change)|"
    r"owned\s+by\s+[\w@ .-]+?\s+(?:for|of)|(?:owns|maintains))\s+(?:the\s+)?"
    r"([A-Za-z_][\w./-]*(?:\s+[A-Za-z_][\w-]*){0,2})", re.I)
_STOP = {"it", "this", "that", "these", "those", "them", "anything", "everything", "here",
         "there", "code", "files", "file", "the", "a", "an", "any", "all", "when", "with"}
_SLASH_WORDS = {"and/or", "either/or", "i/o", "n/a", "w/o", "yes/no", "on/off", "read/write",
                "input/output", "client/server", "true/false", "he/she", "s/he", "ci/cd"}
_HEADING_RX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
_GENERIC_HEADINGS = {"notes", "memory", "overview", "general", "misc", "todo", "context",
                     "guidelines", "rules", "instructions", "conventions", "summary"}


def _clean_term(t: str) -> str:
    t = t.strip().strip("`'\"()[]{}<>,;:!?")
    t = re.sub(r"\.$", "", t)
    if t.startswith("./"):
        t = t[2:]
    return t


def _looks_like_term(t: str) -> bool:
    if not t or len(t) > 120 or t.lower() in _STOP:
        return False
    if " " in t:
        return len(t.split()) <= 3
    return bool(re.search(r"[A-Za-z]", t))


def extract_terms(unit: str) -> list[str]:
    """Paths / identifiers a sentence is about, most specific first."""
    terms: list[str] = []
    for m in _TICK_RX.finditer(unit):
        inner = m.group(1).strip()
        if " " not in inner or len(inner.split()) <= 3:
            terms.append(inner)
    stripped = _TICK_RX.sub(" ", unit)
    for m in _PATH_RX.finditer(stripped):
        val = m.group(1)
        if "/" in val and (not re.search(r"[A-Za-z]", val) or val.lower() in _SLASH_WORDS):
            continue
        if re.match(r"^(?:https?:|www\.)", val) or val.count("/") == 1 and val.endswith("/") \
                and len(val) <= 2:
            continue
        terms.append(val)
    if not terms:
        for m in _PHRASE_RX.finditer(stripped):
            terms.append(m.group(1))
    out: list[str] = []
    for t in terms:
        t = _clean_term(t)
        if _looks_like_term(t) and t not in out and not t.startswith(("http:", "https:")):
            out.append(t)
    return out


def _units(text: str):
    """Yield (heading, unit) for each bullet / line / sentence outside code fences."""
    heading = ""
    in_fence = False
    for raw in text.splitlines():
        if raw.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not raw.strip():
            continue
        h = _HEADING_RX.match(raw)
        if h:
            heading = h.group(2).strip()
            continue
        line = _BULLET_RX.sub("", raw).strip()
        if line.startswith(("---", "|---")):
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`])", line) if len(line) > 160 else [line]
        for p in parts:
            if p.strip():
                yield heading, p.strip()


def signals_from_text(text: str, source: str) -> list[MemorySignal]:
    out: list[MemorySignal] = []
    for heading, unit in _units(text):
        kinds = [k for k, rx in KIND_RX.items() if rx.search(unit)]
        if not kinds:
            continue
        terms = extract_terms(unit)
        if not terms and heading:
            h = _clean_term(heading)
            ht = extract_terms(heading)
            if ht:
                terms = ht
            elif h.lower() not in _GENERIC_HEADINGS and _looks_like_term(h):
                terms = [h]
        snippet = unit if len(unit) <= 300 else unit[:297] + "..."
        for k in kinds:
            for t in terms:
                out.append(MemorySignal(t, k, snippet, source))
    return out


_OWN_REL = re.compile(r"own|maintain|responsible|steward", re.I)
_OWNED_BY = re.compile(r"(?:owned|maintained)_?by|has_?owner|owner$", re.I)


def signals_from_kg(entities: list[dict], relations: list[dict], source: str) -> list[MemorySignal]:
    out: list[MemorySignal] = []
    for e in entities:
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        etype = str(e.get("entityType") or "")
        for obs in e.get("observations") or []:
            obs = str(obs)
            for k, rx in KIND_RX.items():
                if rx.search(obs) or (k != "ownership" and rx.search(etype)):
                    out.append(MemorySignal(name, k, obs[:300], source))
    for r in relations:
        frm, to, rel = (str(r.get(x) or "").strip() for x in ("from", "to", "relationType"))
        if not (frm and to and rel):
            continue
        if _OWNED_BY.search(rel):
            out.append(MemorySignal(frm, "ownership", f"{frm} {rel} {to}", source))
        elif _OWN_REL.search(rel):
            out.append(MemorySignal(to, "ownership", f"{frm} {rel} {to}", source))
    return out


def dedupe(signals: list[MemorySignal]) -> list[MemorySignal]:
    seen, out = set(), []
    for s in signals:
        key = (s.path_or_term.lower(), s.kind, s.text)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


_GENERIC_WORDS = {"module", "modules", "code", "logic", "area", "layer", "component", "components",
                  "package", "folder", "directory", "dir", "file", "files", "the", "system",
                  "subsystem", "part", "stuff", "service", "library", "lib"}


def matches(signal: MemorySignal, rel_path: str) -> bool:
    """Does a signal's path/term apply to a project-relative file path?"""
    term = signal.path_or_term.strip().strip("/")
    rel = rel_path.strip("/")
    if not term:
        return False
    if any(c in term for c in "*?["):
        return fnmatch.fnmatch(rel, term) or fnmatch.fnmatch(rel, "*/" + term)
    if "/" in term or "." in term.rsplit("/", 1)[-1]:
        return rel == term or rel.startswith(term + "/") or rel.endswith("/" + term) \
            or ("/" + term + "/") in ("/" + rel + "/")
    words = [w for w in re.split(r"[\s_.-]+", term.lower()) if w and w not in _GENERIC_WORDS]
    if not words:
        return False
    comps = [c for c in re.split(r"[/_.-]+", rel.lower()) if c]
    if len(words) == 1:
        return words[0] in comps or any(c.startswith(words[0]) and len(words[0]) >= 4
                                        for c in comps)
    joined = "".join(words)
    return joined in "".join(comps) or all(w in comps for w in words)

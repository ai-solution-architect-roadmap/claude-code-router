"""Per-file extraction: symbols, call references, imports and complexity.

tree-sitter (via tree-sitter-language-pack) is used when a grammar is available locally;
otherwise regex heuristics run. Nothing in here may raise for a bad input file.
"""
from __future__ import annotations

import bisect
import hashlib
import logging
import os
import re
from typing import Any

from ccr.graph.files import MAX_PARSE_BYTES, family_of

log = logging.getLogger("ccr.graph")

DEF_KINDS = {"function": "function", "method": "method", "class": "class",
             "interface": "interface", "module": "module", "type": "type", "macro": "macro"}
CLASSLIKE = {"class", "interface", "type", "module", "struct", "impl", "trait", "enum"}
METHOD_PARENTS = CLASSLIKE - {"module"}

NEST_TYPES = [
    "if_statement", "if_expression", "unless", "for_statement", "for_in_statement",
    "for_of_statement", "enhanced_for_statement", "foreach_statement", "for_expression",
    "for_range_loop", "while_statement", "while_expression", "do_statement", "loop_expression",
    "until", "switch_statement", "switch_expression", "expression_switch_statement",
    "type_switch_statement", "select_statement", "match_expression", "match_statement",
    "case_statement", "when_expression", "try_statement", "try_expression", "begin_block",
]
BRANCH_TYPES = [
    "elif_clause", "else_if_clause", "case_clause", "switch_case", "switch_section", "match_arm",
    "when_entry", "catch_clause", "except_clause", "rescue", "conditional_expression",
    "ternary_expression", "boolean_operator", "expression_case", "type_case",
    "communication_case", "when", "elsif",
]
BRANCH_PATTERNS = ['(binary_expression operator: "&&")', '(binary_expression operator: "||")',
                   '(binary_expression operator: "and")', '(binary_expression operator: "or")']
NO_BRANCH = {"try_statement", "try_expression", "begin_block"}
IF_TYPES = {"if_statement", "if_expression"}
ELSE_PARENTS = {"else_clause", "else"}

# Supplemental call patterns for grammars whose bundled tags query has no reference.call.
CALL_PATTERNS = [
    "(call_expression function: (identifier) @name) @reference.call",
    "(call_expression function: (member_expression property: (property_identifier) @name))"
    " @reference.call",
    "(call_expression function: (field_expression field: (field_identifier) @name))"
    " @reference.call",
    "(call_expression function: (selector_expression field: (field_identifier) @name))"
    " @reference.call",
    "(call_expression function: (qualified_identifier name: (identifier) @name))"
    " @reference.call",
    "(invocation_expression function: (identifier) @name) @reference.call",
    "(invocation_expression function: (member_access_expression name: (identifier) @name))"
    " @reference.call",
    "(new_expression constructor: (identifier) @name) @reference.call",
    "(method_invocation name: (identifier) @name) @reference.call",
]

# ----------------------------------------------------------------------------- imports
_R = lambda p: re.compile(p, re.MULTILINE)  # noqa: E731
_PY_FROM = _R(r"^[ \t]*from[ \t]+(\.*[\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n#;]+)")
_PY_IMPORT = _R(r"^[ \t]*import[ \t]+([\w.]+(?:[ \t]+as[ \t]+\w+)?(?:[ \t]*,[ \t]*[\w.]+"
                r"(?:[ \t]+as[ \t]+\w+)?)*)")
_GO_BLOCK = _R(r"^\s*import\s*\(([^)]*)\)")
IMPORT_RX: dict[str, list[tuple[re.Pattern, str]]] = {
    "js": [
        (re.compile(r"\b(?:import|export)\s+(?:type\s+)?[\w\s{},*$]*?\bfrom\s*['\"]([^'\"\n]+)['\"]"), ""),
        (re.compile(r"\bimport\s*['\"]([^'\"\n]+)['\"]"), ""),
        (re.compile(r"\b(?:require|import)\s*\(\s*['\"]([^'\"\n]+)['\"]\s*\)"), ""),
    ],
    "go": [(_R(r"^\s*import\s+(?:[\w.]+\s+)?\"([^\"]+)\""), "")],
    "jvm": [(_R(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)"), "")],
    "rust": [(_R(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+((?:::)?[\w:]+)"), ""),
             (_R(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;"), "mod:")],
    "c": [(_R(r"^\s*#\s*(?:include|import)\s*\"([^\"]+)\""), ""),
          (_R(r"^\s*#\s*(?:include|import)\s*<([^>]+)>"), "<")],
    "ruby": [(_R(r"^\s*require_relative\s*\(?\s*['\"]([^'\"]+)['\"]"), "./"),
             (_R(r"^\s*(?:require|load)\s*\(?\s*['\"]([^'\"]+)['\"]"), "")],
    "php": [(re.compile(r"\b(?:require|include)(?:_once)?\s*\(?\s*['\"]([^'\"]+)['\"]"), ""),
            (_R(r"^\s*use\s+([\w\\]+)"), "ns:")],
    "csharp": [(_R(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;"), "ns:")],
    "zig": [(re.compile(r"@import\(\s*\"([^\"]+)\"\s*\)"), "")],
    "lua": [(re.compile(r"\brequire\s*\(?\s*['\"]([^'\"]+)['\"]"), "")],
    "elixir": [(_R(r"^\s*(?:alias|import|use|require)\s+([A-Z][\w.]*)"), "")],
    "dart": [(_R(r"^\s*(?:import|export|part)\s+['\"]([^'\"]+)['\"]"), "")],
    "swift": [(_R(r"^\s*import\s+(\w+)"), "ns:")],
    "shell": [(_R(r"^\s*(?:source|\.)\s+['\"]?([\w./-]+\.(?:sh|bash))"), "")],
}


def _line_of(text: str, pos: int, starts: list[int]) -> int:
    return bisect.bisect_right(starts, pos)


def extract_imports(text: str, family: str) -> list[tuple[str, int, bool]]:
    """Return (spec, line, optional). Optional specs are dropped when they do not resolve."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    out: list[tuple[str, int, bool]] = []
    if family == "python":
        for m in _PY_FROM.finditer(text):
            mod, names = m.group(1), m.group(2).strip("() \t\n")
            line = _line_of(text, m.start(), starts)
            for item in names.split(","):
                item = item.strip().split()[0] if item.strip() else ""
                if item and item != "*" and item.isidentifier():
                    sep = "" if mod.endswith(".") or not mod else "."
                    out.append((f"{mod}{sep}{item}", line, True))
            out.append((mod, line, False))
        for m in _PY_IMPORT.finditer(text):
            line = _line_of(text, m.start(), starts)
            for part in m.group(1).split(","):
                part = part.strip().split()[0] if part.strip() else ""
                if part:
                    out.append((part, line, False))
        return out
    if family == "go":
        for m in _GO_BLOCK.finditer(text):
            base = _line_of(text, m.start(), starts)
            for sm in re.finditer(r"\"([^\"]+)\"", m.group(1)):
                out.append((sm.group(1), base + m.group(1).count("\n", 0, sm.start()), False))
    for rx, prefix in IMPORT_RX.get(family, []):
        for m in rx.finditer(text):
            out.append((prefix + m.group(1), _line_of(text, m.start(), starts), False))
    return out


# ----------------------------------------------------------------------------- regex symbols
_RX_FUNC = _R(r"^([ \t]*)(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?"
              r"(?:(?:public|private|protected|static|async|inline|extern|override|suspend)\s+)*"
              r"(?:fn|func|function|def|defp|sub|proc|fun)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w!?]*)")
_RX_CLASS = _R(r"^([ \t]*)(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?"
               r"(?:(?:public|private|protected|abstract|final|sealed|data|open|static)\s+)*"
               r"(class|struct|interface|trait|enum|module|object|protocol|impl|defmodule)\s+"
               r"([A-Za-z_][\w.]*)")
_RX_ZIG_TYPE = _R(r"^([ \t]*)(?:pub\s+)?const\s+(\w+)\s*=\s*(?:packed\s+|extern\s+)?"
                  r"(struct|enum|union)\b")
_RX_BRANCH = re.compile(r"\b(?:if|elif|elsif|for|foreach|while|until|case|when|catch|except|"
                        r"rescue|guard|unless)\b|&&|\|\|")


def _indent_nesting(lines: list[str]) -> int:
    indents = []
    for ln in lines:
        s = ln.expandtabs(4)
        stripped = s.lstrip()
        if stripped:
            indents.append(len(s) - len(stripped))
    nz = [i for i in indents if i > 0]
    if not nz:
        return 0
    unit = max(1, min(nz))
    return min(50, max(indents) // unit)


def regex_extract(text: str) -> tuple[list[list], int, int]:
    """Symbols via regex. Returns (symbols, branches, nesting)."""
    lines = text.split("\n")
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    found: list[tuple[int, int, str, str]] = []  # (line, indent, kind, name)
    for m in _RX_CLASS.finditer(text):
        kind = m.group(2)
        kind = "class" if kind not in ("interface", "module", "defmodule") else (
            "interface" if kind == "interface" else "module")
        found.append((_line_of(text, m.start(), starts), len(m.group(1).expandtabs(4)),
                      kind, m.group(3)))
    for m in _RX_ZIG_TYPE.finditer(text):
        found.append((_line_of(text, m.start(), starts), len(m.group(1).expandtabs(4)),
                      "class", m.group(2)))
    for m in _RX_FUNC.finditer(text):
        found.append((_line_of(text, m.start(), starts), len(m.group(1).expandtabs(4)),
                      "function", m.group(2)))
    found.sort()
    symbols: list[list] = []
    stack: list[int] = []  # indices of open class-like symbols
    for i, (line, indent, kind, name) in enumerate(found):
        end = len(lines)
        for nline, nindent, _, _ in found[i + 1:]:
            if nindent <= indent:
                end = max(line, nline - 1)
                break
        while stack and not (symbols[stack[-1]][2] < line <= symbols[stack[-1]][3]):
            stack.pop()
        parent = stack[-1] if stack else -1
        if kind == "function" and parent >= 0 and symbols[parent][0] in METHOD_PARENTS:
            kind = "method"
        body = "\n".join(lines[line - 1:end])
        symbols.append([kind, name, line, end, parent, len(_RX_BRANCH.findall(body)),
                        _indent_nesting(lines[line:end]) if end > line else 0])
        if kind in CLASSLIKE:
            stack.append(len(symbols) - 1)
    return symbols, len(_RX_BRANCH.findall(text)), _indent_nesting(lines)


# ----------------------------------------------------------------------------- tree-sitter
_TS: dict[str, Any] = {}  # lang -> (parser, tags query | None, branch query | None) or None


def _compile_ok(lang_obj, src: str):
    from tree_sitter import Query
    try:
        return Query(lang_obj, src)
    except Exception:  # QueryError, NameError on unknown nodes/fields
        return None


TAGS_INHERIT = {"typescript": ["javascript"], "tsx": ["javascript"]}


def split_patterns(src: str) -> list[str]:
    """Split a query source into top-level patterns (with trailing captures/quantifiers)."""
    out: list[str] = []
    i, n, depth, start = 0, len(src), 0, None
    while i < n:
        c = src[i]
        if c == ";":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == '"':
            i += 1
            while i < n and src[i] != '"':
                i += 2 if src[i] == "\\" else 1
            i += 1
            continue
        if c in "([":
            if depth == 0 and start is None:
                start = i
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth == 0:
                j = i + 1
                while True:  # trailing quantifiers / captures belong to this pattern
                    m = re.match(r"\s*(?:[*+?]|@[\w.-]+)", src[j:])
                    if not m:
                        break
                    j += m.end()
                out.append(src[start:j])
                start, i = None, j
                continue
        i += 1
    return out


def _compile_lenient(lang_obj, src: str):
    q = _compile_ok(lang_obj, src)
    if q is not None or not src.strip():
        return q
    good = [p for p in split_patterns(src) if _compile_ok(lang_obj, p) is not None]
    return _compile_ok(lang_obj, "\n".join(good)) if good else None


def _ts_for(lang: str):
    if lang in _TS:
        return _TS[lang]
    entry = None
    try:
        import tree_sitter_language_pack as tslp
        lang_obj = tslp.get_language(lang)
        parser = tslp.get_parser(lang)
        tags_src = "\n".join(filter(None, [tslp.get_tags_query(x) or ""
                                            for x in TAGS_INHERIT.get(lang, []) + [lang]]))
        tags = _compile_lenient(lang_obj, tags_src) if tags_src.strip() else None
        has_calls = tags is not None and "reference.call" in tags_src
        extra = []
        if not has_calls:
            extra = [p for p in CALL_PATTERNS if _compile_ok(lang_obj, p) is not None]
        calls = _compile_ok(lang_obj, "\n".join(extra)) if extra else None
        nest = [t for t in NEST_TYPES if _compile_ok(lang_obj, f"({t}) @n") is not None]
        branch = [t for t in BRANCH_TYPES if _compile_ok(lang_obj, f"({t}) @b") is not None]
        parts = []
        if nest:
            parts.append("[" + " ".join(f"({t})" for t in nest) + "] @nest")
        branch = [f"({t})" for t in branch]
        branch += [p for p in BRANCH_PATTERNS if _compile_ok(lang_obj, p + " @b") is not None]
        if branch:
            parts.append("[" + " ".join(branch) + "] @branch")
        bq = _compile_ok(lang_obj, "\n".join(parts)) if parts else None
        entry = (parser, tags, calls, bq)
    except Exception as e:  # grammar missing / download failure / ABI mismatch
        log.debug("tree-sitter unavailable for %s: %s", lang, e)
        entry = None
    _TS[lang] = entry
    return entry


_STRUCT_KIND = {"function": "function", "method": "method", "class": "class", "struct": "class",
                "interface": "interface", "enum": "class", "module": "module",
                "trait": "interface", "impl": "class", "namespace": "module"}


def _process_structure(text: str, lang: str) -> list[tuple[int, int, str, str]]:
    """Fallback symbols via tree_sitter_language_pack.process when no tags query exists.

    Returns (start_line, end_line, kind, name)."""
    import tree_sitter_language_pack as tslp
    res = tslp.process(text, tslp.ProcessConfig(language=lang, structure=True, imports=False,
                                                 exports=False))
    out: list[tuple[int, int, str, str]] = []

    def walk(items):
        for it in items:
            raw = str(getattr(it.kind, "name", it.kind)).rsplit(".", 1)[-1].lower()
            kind = _STRUCT_KIND.get(raw)
            if kind and it.name and it.span:
                out.append((it.span.start_line + 1, it.span.end_line + 1, kind, it.name))
            walk(it.children or [])

    walk(res.structure or [])
    return out


def ts_extract(data: bytes, lang: str):
    """Returns (symbols, refs, branches, nesting) or None when tree-sitter is unavailable.

    symbols: [kind, name, start_line, end_line, parent_idx, branches, nesting]
    refs:    (caller_idx or -1, callee name, line, qualified 0/1)
    """
    entry = _ts_for(lang)
    if entry is None:
        return None
    parser, tags, calls_q, bq = entry
    from tree_sitter import QueryCursor
    tree = parser.parse(data)
    root = tree.root_node
    # Positions are byte offsets, or line numbers when symbols come from the line-based
    # process() fallback; containment logic works the same in either unit.
    defs: list[tuple[int, int, str, str, int, int]] = []  # (start, end, kind, name, sl, el)
    refs_raw: list[tuple[int, str, int, int]] = []  # (pos, name, line, qualified)
    seen: set[tuple[int, str]] = set()
    for q in (tags, calls_q):
        if q is None:
            continue
        for _, caps in QueryCursor(q).matches(root):
            names = caps.get("name")
            if not names:
                continue
            nm = names[0]
            name = data[nm.start_byte:nm.end_byte].decode("utf-8", "replace")
            for cname, nodes in caps.items():
                if cname.startswith("definition."):
                    kind = DEF_KINDS.get(cname.split(".", 1)[1])
                    n = nodes[0]
                    if not kind or (n.start_byte, name) in seen:
                        continue
                    seen.add((n.start_byte, name))
                    defs.append((n.start_byte, n.end_byte, kind, name, n.start_point[0] + 1,
                                 n.end_point[0] + 1))
                elif cname == "reference.call":
                    call = nodes[0]
                    qual = (nm.parent is None or nm.parent.id != call.id
                            or call.child_by_field_name("object") is not None
                            or call.child_by_field_name("receiver") is not None)
                    refs_raw.append((call.start_byte, name, nm.start_point[0] + 1, int(qual)))
    line_based = tags is None
    if line_based:
        try:
            structure = _process_structure(data.decode("utf-8", "replace"), lang)
        except Exception:
            structure = []
        if not structure:
            return None  # no tags query and no structure: regex does better here
        defs = [(sl, el + 1, kind, name, sl, el) for sl, el, kind, name in structure]
        refs_raw = [(line, name, line, q) for _, name, line, q in refs_raw]

    def pos(n) -> int:
        return n.start_point[0] + 1 if line_based else n.start_byte

    def end(n) -> int:
        return n.end_point[0] + 2 if line_based else n.end_byte

    # complexity: decision points and control-structure nesting
    nests: list[tuple[int, int]] = []  # (pos, depth)
    branch_pos: list[int] = []
    max_depth = 0
    if bq is not None:
        caps = QueryCursor(bq).captures(root)
        items = [(n.start_byte, -n.end_byte, "nest", n) for n in caps.get("nest", [])]
        items += [(n.start_byte, -n.end_byte, "branch", n) for n in caps.get("branch", [])]
        items.sort(key=lambda x: (x[0], x[1]))
        stack: list[int] = []
        for sb, neb, cap, n in items:
            if cap == "branch":
                branch_pos.append(pos(n))
                continue
            while stack and stack[-1] <= sb:
                stack.pop()
            if n.type not in NO_BRANCH:
                branch_pos.append(pos(n))
            if n.type in IF_TYPES and n.parent is not None and n.parent.type in ELSE_PARENTS:
                continue  # else-if chains do not deepen nesting
            stack.append(-neb)
            nests.append((pos(n), len(stack)))
            max_depth = max(max_depth, len(stack))
    branch_pos.sort()
    nests.sort()
    nest_pos = [p for p, _ in nests]

    # symbols in start order; parent = innermost enclosing symbol
    defs.sort(key=lambda d: (d[0], -d[1]))
    symbols: list[list] = []
    spans: list[tuple[int, int]] = []
    st: list[int] = []
    for s, e, kind, name, sl, el in defs:
        while st and not (spans[st[-1]][0] <= s and e <= spans[st[-1]][1]):
            st.pop()
        parent = st[-1] if st else -1
        if kind == "function" and parent >= 0 and symbols[parent][0] in METHOD_PARENTS:
            kind = "method"
        nb = bisect.bisect_left(branch_pos, e) - bisect.bisect_left(branch_pos, s)
        depths = [d for _, d in nests[bisect.bisect_left(nest_pos, s):
                                      bisect.bisect_left(nest_pos, e)]]
        nd = (max(depths) - min(depths) + 1) if depths else 0
        symbols.append([kind, name, sl, el, parent, nb, nd])
        spans.append((s, e))
        st.append(len(symbols) - 1)

    starts = [sp[0] for sp in spans]
    refs: list[tuple[int, str, int, int]] = []
    for p, name, line, qual in refs_raw:
        k = bisect.bisect_right(starts, p) - 1
        while k >= 0 and not (spans[k][0] <= p < spans[k][1]):
            k = symbols[k][4]
        refs.append((k, name, line, qual))
    return symbols, refs, len(branch_pos), max_depth


# ----------------------------------------------------------------------------- entry point
_ALLOWED: frozenset[str] | None = None


def init_worker(allowed: frozenset[str] | None) -> None:
    global _ALLOWED
    _ALLOWED = allowed


def file_hash(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def parse_file(job: tuple[str, str, str | None, str | None]) -> dict:
    """job = (root, rel, ts_language or None, previous hash or None)."""
    root, rel, lang, old_hash = job
    res: dict = {"rel": rel, "status": "error", "lang": lang or "", "parser": "none",
                 "symbols": [], "refs": [], "imports": [], "loc": 0, "branches": 0,
                 "nesting": 0, "size": 0, "mtime_ns": 0, "hash": None}
    path = os.path.join(root, rel)
    try:
        st = os.stat(path)
        res["size"], res["mtime_ns"] = st.st_size, st.st_mtime_ns
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return res
    h = file_hash(data)
    res["hash"] = h
    if old_hash is not None and h == old_hash:
        res["status"] = "unchanged"
        return res
    if b"\0" in data[:8192]:
        res["status"] = "skipped"
        return res
    res["status"] = "parsed"
    res["loc"] = sum(1 for ln in data.split(b"\n") if ln.strip())
    if len(data) > MAX_PARSE_BYTES:
        res["parser"] = "size-only"
        return res
    text = data.decode("utf-8", "replace")
    try:
        res["imports"] = extract_imports(text, family_of(rel))
    except Exception as e:
        log.debug("import extraction failed for %s: %s", rel, e)
    out = None
    if lang and (_ALLOWED is None or lang in _ALLOWED):
        try:
            out = ts_extract(data, lang)
        except Exception as e:
            log.debug("tree-sitter failed for %s: %s", rel, e)
            out = None
    if out is not None:
        res["symbols"], res["refs"], res["branches"], res["nesting"] = out
        res["parser"] = "tree-sitter"
        return res
    try:
        res["symbols"], res["branches"], res["nesting"] = regex_extract(text)
        res["parser"] = "regex"
    except Exception as e:
        log.debug("regex extraction failed for %s: %s", rel, e)
        res["parser"] = "size-only"
    return res


def parse_many(jobs: list[tuple], allowed: frozenset[str] | None) -> list[dict]:
    init_worker(allowed)
    return [parse_file(j) for j in jobs]

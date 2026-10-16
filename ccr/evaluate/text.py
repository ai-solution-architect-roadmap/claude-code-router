"""Prompt tokenization shared by the classifier and the locator (stdlib only)."""
from __future__ import annotations

import re

_EXT = (r"py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|scala|rb|php|cs|c|h|cc|cpp|hpp|swift|"
        r"sql|ya?ml|json|toml|ini|cfg|md|rst|txt|sh|zig|ex|exs|lua|dart|vue|svelte|proto|tf|"
        r"tfvars|mod|sum|lock|gradle|xml|html|css|scss|env|dockerfile")
# @path mentions (Claude Code file references), e.g. @src/app.ts or @README.md
AT_RX = re.compile(r"(?<![\w@])@((?:\.{0,2}/)?[\w.\-]+(?:/[\w.\-]+)*/?)")
# bare path-like tokens: a/b/c, ./x, file.ext
PATH_RX = re.compile(
    r"(?<![\w@/.\-])((?:\.{1,2}/)?(?:[\w.\-]+/)+[\w.\-]*|[\w\-]+(?:\.[\w\-]+)*\.(?:" + _EXT +
    r"))(?![\w/])", re.I)
TICK_RX = re.compile(r"`([^`\n]{1,120})`")
URL_RX = re.compile(r"\bhttps?://\S+|\bwww\.\S+", re.I)
QUOTED_RX = re.compile(r"\"[^\"\n]{3,200}\"|'[^'\n]{6,200}'")
IDENT_RX = re.compile(
    r"\b(?:[A-Za-z_]\w*(?:\.|::|#)[A-Za-z_]\w*"              # Engine.run / store::get
    r"|[A-Za-z_]\w*(?=\(\))"                                  # paginate()
    r"|[a-z]+[A-Z]\w*|[A-Z][a-z0-9]+[A-Z]\w*"                  # camelCase / CamelCase
    r"|[A-Za-z]\w*_\w+"                                        # snake_case
    r"|[A-Z][a-z0-9]{2,})\b(?:\(\))?")                        # Capitalized (Button, Engine)
WORD_RX = re.compile(r"[a-z0-9][a-z0-9+\-]*[a-z0-9]|[a-z0-9]", re.I)

_SLASH_WORDS = {"and/or", "either/or", "i/o", "n/a", "w/o", "yes/no", "on/off", "read/write",
                "input/output", "client/server", "true/false", "ci/cd", "async/await"}

STOP = frozenset("""
a an the and or but nor so yet to of in on at by for with from into onto over under about as
is are was were be been being am it its it's this that these those there here then than
what where which who whom whose when why how do does did done doing can could should would
will shall may might must please pls let lets let's i me my we our us you your they them their
he she his her not no yes also just only very more less much many few some any each every all
both either neither one two three first last next previous same other another such own
make makes made sure need needs want wants like get gets got set sets put use uses using used
add adds added fix fixes fixed change changes changed update updates updated create creates
implement write remove delete rename move edit modify improve handle handles support
new old existing current now still again instead without within via per etc
code codes file files function functions method methods class classes module modules
bug bugs error errors issue issues problem problems feature features test tests testing
line lines value values data type types variable variables var name names thing things stuff
project repo repository codebase app application system logic way part work works working
broken wrong bad good better best simple small large big quick quickly whole entire
everywhere across throughout something somehow anything everything nothing whatever
explain describe tell show find look see check run runs running call calls called return
returns returned page help okay ok hey hi thanks thank
""".split())

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


# "in this session", "earlier in the session": the Claude Code conversation, not an auth
# session. Kept when a security noun follows ("this session token").
CONVERSATION_RX = re.compile(
    r"\b(?:(?:this|that|current|same|previous|last|whole|entire|prior)\s+"
    r"(?:(?:claude|chat|coding|work)\s+)?session"
    r"|(?:earlier|later|before)\s+in\s+(?:the|this)\s+session)\b"
    r"(?!\s+(?:tokens?|cookies?|ids?|keys?|secrets?|fixation|hijack\w*|management|expir\w*|"
    r"invalidation|store|storage|data|handling|validation|timeouts?))", re.I)


def strip_refs(text: str) -> str:
    """Text with URLs, @mentions, paths, backticks, quoted strings and conversational
    "this session" references blanked out."""
    t = CONVERSATION_RX.sub(" ", text)
    t = URL_RX.sub(" ", t)
    t = TICK_RX.sub(" ", t)
    t = QUOTED_RX.sub(" ", t)
    t = AT_RX.sub(" ", t)
    t = PATH_RX.sub(lambda m: m.group(0) if m.group(0).lower() in _SLASH_WORDS else " ", t)
    return t


def path_tokens(text: str) -> tuple[list[str], list[str]]:
    """(@-mentioned paths, other path-like tokens), cleaned and de-duplicated."""
    text = URL_RX.sub(" ", text)
    ats = _uniq(_clean_path(m.group(1)) for m in AT_RX.finditer(text))
    rest = AT_RX.sub(" ", text)
    found = []
    for m in TICK_RX.finditer(rest):
        inner = m.group(1).strip()
        if " " not in inner and ("/" in inner or PATH_RX.fullmatch(inner)):
            found.append(inner)
    for m in PATH_RX.finditer(TICK_RX.sub(" ", rest)):
        v = m.group(1)
        if v.lower() in _SLASH_WORDS or not re.search(r"[A-Za-z]", v):
            continue
        if re.fullmatch(r"\d+(\.\d+)+", v) or v.endswith("/") and len(v) <= 2:
            continue
        found.append(v)
    paths = _uniq(_clean_path(p) for p in found)
    return [a for a in ats if a], [p for p in paths if p and p not in ats]


def identifiers(text: str) -> list[str]:
    """Symbol-shaped tokens: backticked names, Dotted.names, calls(), camelCase, snake_case,
    Capitalized words that are not the first word of a sentence."""
    out = []
    for m in TICK_RX.finditer(text):
        inner = m.group(1).strip().rstrip("()")
        if inner and " " not in inner and "/" not in inner and not PATH_RX.fullmatch(inner):
            out.append(inner)
    body = TICK_RX.sub(" ", URL_RX.sub(" ", text))
    body = AT_RX.sub(" ", body)
    body = PATH_RX.sub(" ", body)
    for m in IDENT_RX.finditer(body):
        tok = m.group(0).rstrip("()")
        start = m.start()
        before = body[:start].rstrip()
        sentence_start = not before or before[-1] in ".!?;:\n"
        if re.fullmatch(r"[A-Z][a-z0-9]{2,}", tok) and (sentence_start or tok.lower() in STOP):
            continue
        out.append(tok)
    return _uniq(out)


def words(text: str) -> list[str]:
    return [w.lower() for w in WORD_RX.findall(text)]


def content_words(text: str, min_len: int = 3) -> list[str]:
    return _uniq(w for w in words(strip_refs(text)) if len(w) >= min_len and w not in STOP
                 and not w.isdigit())


def stem(w: str) -> str:
    w = w.lower()
    for suf in ("ies", "ing", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            if suf == "ies":
                return w[:-3] + "y"
            if suf == "es" and not w.endswith(("sses", "xes", "ches", "shes", "zes")):
                return w[:-1]
            return w[: -len(suf)]
    return w


def split_ident(name: str) -> list[str]:
    """Components of a path segment or identifier: useAuth.ts -> [use, auth]."""
    parts = []
    for chunk in re.split(r"[/_.\-\s]+", name):
        if chunk:
            parts += [p.lower() for p in _CAMEL.sub(" ", chunk).split()]
    return parts


def phrase_in(phrase: str, text_words: list[str]) -> bool:
    """Whole-word (stemmed) phrase containment."""
    pw = [stem(w) for w in words(phrase)]
    if not pw:
        return False
    tw = [stem(w) for w in text_words]
    n = len(pw)
    return any(tw[i:i + n] == pw for i in range(len(tw) - n + 1))


def _clean_path(p: str) -> str:
    p = p.strip().strip("`'\"()[]{}<>,;:!?")
    p = re.sub(r"[.:]+$", "", p) if not p.endswith("/") else p
    while p.startswith("./"):
        p = p[2:]
    return p


def _uniq(items) -> list:
    seen, out = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


RETRY = re.compile(
    r"\b(?:still\s+(?:fail\w*|broken|wrong|getting|erroring|crash\w*|red|"
    r"not\s+(?:work\w*|fix\w*|pass\w*|right|correct|compil\w*|load\w*|show\w*|resolved)|"
    r"the\s+same|happening|there|seeing)"
    r"|(?:did\s*n[o']?t|does\s*n[o']?t|doesn'?t|didn'?t|isn'?t|is\s+not|wasn'?t)\s+"
    r"(?:work\w*|fix\w*|help\w*|solve\w*|change\w*)"
    r"|not\s+(?:fixed|working|solved|resolved)"
    r"|same\s+(?:error|problem|issue|failure|exception|bug|result)"
    r"|(?:that|this|it)\s+(?:broke|made\s+it\s+worse)"
    r"|(?:try|do\s+it)\s+again|no\s+luck|wrong\s+again|regress\w*|nothing\s+changed"
    r"|(?:tests?|build|ci)\s+(?:still\s+)?(?:fail\w*|red|broken))", re.I)


def is_retry(prompt: str) -> bool:
    """"still failing", "that did not work": a follow-up about the previous attempt."""
    return bool(RETRY.search(prompt[:400]))

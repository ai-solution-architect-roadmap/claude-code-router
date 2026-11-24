"""Rule-based task classification: class, scope, vagueness, destructive / hard markers.

Token-free and deterministic. Each class has weighted patterns; the score of a pattern grows
(sub-linearly) with the number of distinct terms it matched. Paths, @mentions, backticks and
quoted strings are blanked out first so `app/auth/x.py` does not read as a security task.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ccr.evaluate import text as T

_I = re.I

_POLITE = re.compile(
    r"^\s*(?:(?:hey|hi|ok(?:ay)?|so|now|then|also|claude)[,!.:]?\s+)*"
    r"(?:please\s+|pls\s+|kindly\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|will\s+you\s+|"
    r"i\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+|i'?d\s+like\s+(?:you\s+)?to\s+|"
    r"help\s+me\s+(?:to\s+)?|let'?s\s+|we\s+(?:need|have)\s+to\s+|go\s+ahead\s+and\s+)+", _I)

# class -> [(pattern, weight)]
RULES: dict[str, list[tuple[re.Pattern, float]]] = {
    "question": [
        (re.compile(r"^\s*(?:what|where|which|who|when|how|wheres|where'?s|what'?s|how'?s|is|are|does|do\s+(?:i|we|you)|"
                    r"did|can\s+(?:i|we)|should\s+(?:i|we)|explain|describe|summari[sz]e|"
                    r"tell\s+me|show\s+me|list|walk\s+me\s+through)\b", _I), 2.0),
        (re.compile(r"\?\s*$"), 1.5),
        (re.compile(r"^\s*(?:help|hi|hello|hey)\s*[!?.]*\s*$", _I), 4.0),
        # "why does X ...?" asks for an explanation; debugging words decide otherwise
        (re.compile(r"^\s*why\b[^?]*\?", _I), 2.0),
        (re.compile(r"\b(?:explain|describe|summari[sz]e|walk\s+me\s+through|overview\s+of|"
                    r"what\s+does|how\s+does|how\s+do(?:es)?\s+\w+\s+work|where\s+is|where\s+are|"
                    r"which\s+files?|find\s+where|look\s+up|understand|what\s+is|what\s+are|"
                    r"how\s+is|how\s+are)\b", _I), 2.0),
    ],
    "trivial": [
        (re.compile(r"\b(?:typos?|spelling|misspell\w*|renam\w*|reword\w*|wording|comments?|"
                    r"docstrings?|readme|changelog|whitespace|indent\w*|formatting|reformat|"
                    r"lint(?:ing)?|bump|version\s+number|copy\s?text|label|colou?r|"
                    r"log\s+message|sort\s+imports|import\s+order|unused\s+imports?|"
                    r"dead\s+code|one[- ]liner|license\s+header|todo|"
                    r"unused\s+(?:[\w-]+\s+){0,2}?(?:variables?|functions?|methods?|parameters?|params?|arguments?|"
                    r"args?|constants?|fields?|helpers?|classes|class|code|locals?))\b", _I), 2.0),
        (re.compile(r"\b(?:add|delete|remove|drop)\s+(?:a\s+|an\s+|the\s+)?(?:short\s+|trailing\s+|blank\s+)?"
                    r"(?:comments?|docstrings?|type\s+hints?|newlines?|blank\s+lines?|whitespace|"
                    r"license\s+header|commented[- ]out\s+code)\b|\bcommented[- ]out\b|"
                    r"\btrailing\s+(?:newline|whitespace)\b", _I), 3.0),
        (re.compile(r"\b(?:change|update|set|fix)\s+(?:the\s+)?(?:[\w-]+\s+){0,2}?"
                    r"(?:text|label|title|wording|copy|colou?r)\b|\bcapitali[sz]\w*|\bwording\b", _I), 3.0),
    ],
    "tests": [
        (re.compile(r"\b(?:add|write|create|increase|improve|extend|cover)\b[\w\s,-]{0,40}?"
                    r"\b(?:unit\s+|integration\s+|e2e\s+|regression\s+|snapshot\s+)?tests?\b",
                    _I), 3.0),
        (re.compile(r"\btest\s+coverage\b|\btest\s+cases?\b", _I), 2.0),
    ],
    "bugfix": [
        (re.compile(r"\b(?:fix\w*|bugs?|broken|crash\w*|"
                    r"(?:errors?|exceptions?)(?!\s+(?:messages?|handling|handlers?|texts?|strings?|"
                    r"copy|labels?|wording|pages?|codes?|types?|classes|class))|fails?|failing|"
                    r"failure|incorrect\w*|wrong|regression|null|nil|nan|undefined|"
                    r"off[- ]by[- ]one|doesn'?t\s+work|not\s+working|traceback|panic\w*|"
                    r"segfault|returns?\s+(?:none|null|nil|wrong|empty)|double[- ]count\w*)\b",
                    _I), 2.0),
    ],
    "feature": [
        (re.compile(r"\b(?:add|implement|create|build|support|introduce|new|enable|allow|"
                    r"expose|integrate|endpoint|option|flag|feature|page|component|prop|"
                    r"command|subcommand|toggle|button|api)\b", _I), 1.5),
    ],
    "refactor": [
        (re.compile(r"\b(?:refactor\w*|restructur\w*|reorgani[sz]\w*|clean\s?up|cleanup|"
                    r"extract|split|decouple|simplif\w*|dedup\w*|de-?duplicat\w*|"
                    r"consolidat\w*|modulari[sz]\w*|rewrite|tidy|untangle|"
                    r"smaller\s+(?:functions|components|modules|pieces))\b", _I), 3.0),
    ],
    "migration": [
        (re.compile(r"\b(?:migrat\w*|upgrade|port\s+(?:it\s+)?(?:to|from)|convert\w*\s+[\w\s]{1,40}?"
                    r"\s+(?:to|into)|move\s+from\s+\w+\s+to|switch\s+(?:from|over)|"
                    r"schema\s+changes?|alter\s+table|alembic|backfill\w*|deprecat\w*)\b",
                    _I), 3.5),
        # schema changes in plain words: "add a nullable notes column to products"
        (re.compile(r"\b(?:add|create|drop|remove|rename|alter|change)\b[\w\s,'\"-]{0,30}?"
                    r"\b(?:columns?|tables?|unique\s+constraints?|foreign\s+keys?|primary\s+keys?|"
                    r"constraints?|index(?:es)?\s+(?:on|to|for)|indices)\b", _I), 3.5),
        # dropping schema objects is a migration even without the word
        (re.compile(r"\b(?:drop|truncate)\s+(?:the\s+)?(?:[\w.]+\s+){0,3}?"
                    r"(?:columns?|tables?|databases?|schemas?)\b", _I), 3.5),
    ],
    "security": [
        (re.compile(r"\b(?:security|secure|insecure|vulnerab\w*|cve-?\d*|exploit\w*|xss|csrf|"
                    r"ssrf|injection|sqli|rce|sanitiz\w*|privilege\s+escalation|hard-?cod\w*|"
                    r"leak(?:s|ed|ing)?\s+(?:secrets?|credentials?|tokens?|keys?|pii)|"
                    r"audit|pen-?test\w*|threat\s+model)\b", _I), 3.0),
        (re.compile(r"\b(?:session\s+fixation|fixation|csrf|clickjack\w*|brute[- ]?force|"
                    r"lock\s*down|access\s+control|authori[sz]\w*|"
                    r"tokens?\s+(?:never\s+)?expir\w*|admin\s+routes?|two[- ]factor|2fa|mfa)\b",
                    _I), 4.0),
        (re.compile(r"\b(?:auth\w*|permissions?|secrets?|credentials?|passwords?|encrypt\w*|"
                    r"decrypt\w*|crypto\w*|jwt|signature\s+verification|rotate|tls|cors|"
                    r"api\s+keys?)\b", _I), 1.0),
    ],
    "perf": [
        (re.compile(r"\b(?:perf\b|performance|slow\w*|speed\s?up|faster|latency|throughput|"
                    r"optimi[sz]\w*|bottleneck\w*|profil\w*|caching|n\+1|memory\s+usage|"
                    r"cpu|benchmark\w*|hot\s+path|allocations?)\b", _I), 2.5),
    ],
    "debugging": [
        (re.compile(r"\b(?:debug\w*|investigat\w*|diagnos\w*|root\s+cause|figure\s+out\s+why|"
                    r"find\s+out\s+why|intermittent\w*|flaky|flakiness|sometimes|randomly|"
                    r"occasionally|sporadic\w*|race\s+conditions?|data\s+races?|deadlock\w*|"
                    r"hangs?|hanging|leak(?:s|ing)?|memory\s+leak|heisenbug|"
                    r"can'?t\s+reproduce|non-?deterministic\w*|keeps?\s+(?:growing|crashing|"
                    r"failing))\b", _I), 3.0),
        # "why does X crash" stated as a problem, not asked as a question (see WHY_QUESTION)
        (re.compile(r"^\s*why\b(?![^?]*\?)", _I), 2.0),
        # symptoms with no cause named: the work is finding out what is wrong
        (re.compile(r"\b(?:no\s+idea\s+why|(?:do\s?n'?t|not)\s+(?:know|sure)\s+why|at\s+random|"
                    r"random\s+\w+|now\s+and\s+then|at\s+times|(?:do(?:es)?n'?t|do(?:es)?\s+not)\s+add\s+up|"
                    r"numbers?\s+(?:are\s+|look\s+)?(?:off|wrong)|(?:feels?|seems?|looks?)\s+(?:off|broken|wrong)|"
                    r"(?:wrong|incorrect|off)\s+(?:totals?|numbers?|amounts?)|keeps?\s+\w+ing|"
                    r"breaks?\s+again|users?\s+(?:are\s+|keep\s+)?(?:report|complain)\w*|customers?\s+(?:report|complain|see|get)\w*|"
                    r"duplicate\s+\w+|twice)\b", _I), 3.0),
    ],
    "design": [
        (re.compile(r"\b(?:design|outline|sketch|propose|draft)\s+(?:me\s+)?(?:a|an|the|how)\b|"
                    r"\b(?:architecture|design|rollout|roadmap|migration|adoption)\s+(?:plan|for|of)\b|"
                    r"\b(?:come\s+up\s+with|give\s+me|write)\s+(?:a\s+)?(?:plan|strategy|design|roadmap)\b|"
                    r"\bhow\s+(?:should|could|would)\s+(?:we|you|i)\s+(?:structure|split|design|organi[sz]e|"
                    r"migrate|handle|approach|architect|build|support)\b|"
                    r"\bbest\s+(?:way|approach)\s+to\s+(?:add|build|structure|support|handle|implement)\b|"
                    r"\bwhat\s+would\s+a\s+good\s+(?:architecture|design|approach)\b|"
                    r"\b(?:strategy|plan)\s+for\b", _I), 4.0),
    ],
}

_SCHEMA_PLAIN = RULES["migration"][1][0]    # "add a ... column to ..." without the word migration

# A leading imperative verb is the strongest single cue.
LEAD_VERB = [
    (re.compile(r"^\s*(?:fix|repair|resolve|patch)\b", _I), "bugfix", 1.5),
    (re.compile(r"^\s*(?:add|implement|create|build|introduce|support|enable|expose|make)\b",
                _I), "feature", 1.0),
    (re.compile(r"^\s*(?:refactor|restructure|reorgani[sz]e|clean|extract|split|simplify|"
                r"rewrite|tidy|decouple)\b", _I), "refactor", 1.5),
    (re.compile(r"^\s*(?:migrate|upgrade|port|convert)\b", _I), "migration", 1.5),
    (re.compile(r"^\s*(?:debug|investigate|diagnose|figure\s+out|find\s+out)\b", _I),
     "debugging", 1.5),
    (re.compile(r"^\s*(?:optimi[sz]e|speed\s+up|profile)\b", _I), "perf", 1.5),
    (re.compile(r"^\s*(?:rename|bump|reformat|format|reword|update\s+the\s+(?:readme|docs?|"
                r"changelog|comment))\b", _I), "trivial", 1.5),
    (re.compile(r"^\s*(?:audit|harden|secure)\b", _I), "security", 1.5),
    (re.compile(r"^\s*(?:plan|design|architect)\b", _I), "design", 1.5),
]

# Context rules: words whose meaning depends on the rest of the prompt.
# "why does X ...?" is a question unless it names a failure.
WHY_QUESTION = re.compile(r"^\s*why\b[^?]*\?", _I)
BUG_WORDS = re.compile(
    r"\b(?:crash\w*|fail\w*|errors?|exceptions?|broken|bugs?|flak(?:y|iness)|intermittent\w*|"
    r"sometimes|randomly|occasional\w*|sporadic\w*|hangs?|hanging|leak\w*|race\s+conditions?|"
    r"deadlock\w*|wrong|incorrect\w*|null|nan|undefined|traceback|panic\w*|segfault)\b", _I)
# "fix the typo in the error message" is a typo fix, not a bug fix
TYPO = re.compile(r"\b(?:typos?|spelling|misspell\w*)\b", _I)
TYPO_BONUS = 2.0
# rate limiting is security work on auth / API surfaces, ordinary feature work elsewhere
RATE_LIMIT = re.compile(r"\brate[- ]?limit\w*", _I)
AUTH_API = re.compile(
    r"\b(?:auth\w*|log\s?ins?|logouts?|sign[- ]?(?:in|up|on)|passwords?|credentials?|tokens?|"
    r"sessions?|otp|2fa|mfa|api|apis|endpoints?|routes?|brute[- ]?force|abuse)\b", _I)
# "add a column to the dashboard table" is UI work unless database words say otherwise
UI_CONTEXT = re.compile(
    r"\b(?:dashboards?|pages?|screens?|views?|ui|ux|grids?|components?|frontend|front-end|html|"
    r"csv|spreadsheets?|excel|reports?|widgets?|modals?|forms?|display\w*|render\w*|layouts?|"
    r"sidebars?|panels?|charts?|markdown|output)\b", _I)
DB_CONTEXT = re.compile(
    r"\b(?:databases?|db|sql|postgres\w*|mysql|sqlite|schemas?|migrations?|nullable|"
    r"not\s+null|foreign\s+keys?|primary\s+keys?|constraints?|alembic|prisma|orm|models?|"
    r"alter|varchar|index(?:es)?|indices)\b", _I)
# "update/upgrade X to 1.2" is a dependency bump; frameworks, runtimes, databases and major
# versions ("to 5", "major") stay migrations
MINOR_BUMP = re.compile(
    r"^\s*(?P<verb>update|upgrade)\s+(?:the\s+)?(?P<what>(?:[\w.@/+-]+\s+){1,3}?)"
    r"(?:(?:dependency|package|version)\s+)?(?:from\s+v?\d[\w.]*\s+)?to\s+v?\d+(?:\.\d+)+\b", _I)
PLATFORM = re.compile(
    r"\b(?:django|rails|react|angular|vue|next(?:\.?js)?|nuxt|svelte|spring|flask|fastapi|"
    r"express|laravel|symfony|dotnet|\.net|python|node(?:\.?js)?|java|jdk|php|ruby|go|golang|"
    r"rust|typescript|kotlin|swift|postgres\w*|mysql|mariadb|mongo\w*|redis|sqlite|"
    r"elasticsearch|kafka|kubernetes|k8s|terraform|schema|database|db|major)\b", _I)


def _as_bump(core: str) -> str:
    """`core` with a plain "update/upgrade <dependency> to <x.y>" read as "bump ..."."""
    m = MINOR_BUMP.search(core)
    if not m or PLATFORM.search(core):
        return core
    return core[:m.start("verb")] + "bump" + core[m.end("verb"):]


SWEEPING = re.compile(
    r"\b(?:everything|everywhere|the\s+whole\s+(?:thing|app|backend|frontend|stack|project)|"
    r"rewrite\s+(?:the\s+)?(?:web\s+)?(?:app|backend|frontend|site)\b|all\s+(?:of\s+)?the\s+code|"
    r"monorepo|micro-?services?|serverless|multi[- ]?region|"
    r"entire\s+(?:app|backend|frontend|codebase|project))\b", _I)
SCOPE_LARGE = re.compile(
    r"\b(?:everything|multi[- ]?region|monorepo|micro-?services?|serverless|"
    r"entire|whole|everywhere|codebase|code\s+base|project-?wide|repo(?:sitory)?-?wide|"
    r"system-?wide|throughout|globally|all\s+over|architecture|overall|"
    r"across\s+(?:the\s+)?(?:codebase|project|repo|app|services|modules|packages|files)|"
    r"all\s+(?:the\s+)?(?:files|modules|components|endpoints|services|tests|usages|call\s?sites|"
    r"places|occurrences|packages|handlers|routes|models|class\s+components)|"
    r"every\s+(?:file|module|component|endpoint|service|call|handler|route|model|usage))\b", _I)
SCOPE_SMALL = re.compile(
    r"\b(?:small|tiny|minor|quick(?:ly)?|simple|trivial|just|single|one[- ]liner|one[- ]line|"
    r"one\s+(?:function|file|place|spot|method)|this\s+(?:function|method|line)|"
    r"a\s+few\s+lines|slight(?:ly)?)\b", _I)
DESTRUCTIVE = re.compile(
    r"\b(?:drop(?:s|ping|ped)?\s+(?:the\s+)?(?:[\w.]+\s+){0,3}?(?:columns?|tables?|"
    r"index(?:es)?|databases?|collections?|fields?|schemas?|users?|records?|rows?)|truncat\w*|"
    r"irreversibl\w*|backfill\w*|in\s+production|"
    r"(?:on|in|against|from)\s+(?:the\s+)?(?:prod|production|live)\b|"
    r"production\s+(?:data|database|db)|live\s+(?:data|db|database)|prod\s+(?:data|db)|"
    r"wipe|purge|(?:delete|remove|erase|clear|empty)\s+(?:all|every)|"
    r"force[- ]push|rewrite\s+history|data\s+loss)\b", _I)
HARD = re.compile(
    r"\b(?:race\s+conditions?|data\s+races?|deadlock\w*|concurren\w*|thread[- ]safe\w*|"
    r"lock\s+contention|goroutine\s+leaks?|memory\s+leaks?|distributed|idempoten\w*|"
    r"transactional|consistency|"
    # intermittent failures: reproduction and diagnosis dominate the work, not the edit
    r"sometimes|intermittent\w*|flak(?:y|iness|e)|randomly|occasional\w*|sporadic\w*|"
    r"every\s+now\s+and\s+then|once\s+in\s+a\s+while|only\s+under\s+load|"
    r"works?\s+on\s+my\s+machine)\b", _I)
VAGUE = re.compile(
    r"\b(?:something|somehow|stuff|things|whatever|etc|better|improve|nicer|cleaner|properly|"
    r"make\s+it\s+work|doesn'?t\s+work|not\s+working|weird|some\s+issues?|"
    r"the\s+code|the\s+app|fix\s+it|look\s+into)\b", _I)
# explicit step-by-step instructions: the user already did the planning, so the work is
# execution, not exploration. Numbered/"step N" lines, or a first ... then ... finally chain.
STEP_LINE = re.compile(r"^\s*(?:\(?\d{1,2}[.):]|step\s*\d{1,2}\b)", _I | re.M)
STEP_WORDS = re.compile(r"\b(?:first(?:ly)?|then|next|after\s+that|finally|lastly)\b", _I)
MIN_STEPS = 3


def count_steps(text: str) -> int:
    """Number of explicit steps in the prompt (0 when it is not a step-by-step list)."""
    n = len(STEP_LINE.findall(text))
    if n < MIN_STEPS:
        n = max(n, _inline_steps(text))
    if n < MIN_STEPS:
        found = [w.lower() for w in STEP_WORDS.findall(text)]
        n = len(found) if found and found[0].startswith("first") else 0
    return n if n >= MIN_STEPS else 0


INLINE_STEP = re.compile(r"(?:^|(?<=\s))\(?(\d{1,2})[.)]\s+(?=\S)")


def _inline_steps(text: str) -> int:
    """Length of a 1. 2. 3. ... run written on one line (consecutive numbers from 1)."""
    n = 0
    for m in INLINE_STEP.finditer(text):
        if int(m.group(1)) == n + 1:
            n += 1
    return n


@dataclass
class TaskClass:
    kind: str
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)
    matched: dict[str, list[str]] = field(default_factory=dict)
    secondary: str | None = None
    scope: str = "unspecified"           # small | medium | large | unspecified
    scope_terms: list[str] = field(default_factory=list)
    steps: int = 0                       # explicit step-by-step instructions given (0 = none)
    vagueness: float = 0.0
    vague_terms: list[str] = field(default_factory=list)
    destructive: list[str] = field(default_factory=list)
    sweeping: list[str] = field(default_factory=list)
    words: int = 0
    hard: list[str] = field(default_factory=list)
    source: str = "rules"                # rules | llm

    @property
    def vague(self) -> bool:
        return self.vagueness >= 0.6

    def evidence_terms(self) -> list[str]:
        """Matched cue words for the chosen class, for the history trace: short rule
        vocabulary only (at most 4 terms of 24 characters)."""
        return [t for t in (self.matched.get(self.kind) or []) if len(t) <= 24][:4]

    def evidence(self, kind: str | None = None) -> str:
        terms = self.matched.get(kind or self.kind) or []
        return ", ".join(f"'{t}'" for t in terms[:4])


def _hits(rx: re.Pattern, text: str) -> list[str]:
    seen = []
    for m in rx.finditer(text):
        t = re.sub(r"\s+", " ", m.group(0).strip().lower())
        if t and t not in seen:
            seen.append(t)
    return seen


def _extra_hard(terms: list[str]) -> re.Pattern | None:
    words = [re.escape(t).replace(r"\ ", r"\s+") for t in terms if t]
    return re.compile(r"\b(?:" + "|".join(words) + r")s?\b", _I) if words else None


def classify(task: str, located: bool | None = None,
             hard_terms: list[str] | None = None) -> TaskClass:
    """Classify a task prompt. `located`: whether code was located (feeds vagueness).
    `hard_terms`: project words (config `hard_terms`) that count like "intermittent"."""
    raw = task.strip()
    core = _as_bump(_POLITE.sub("", raw, count=1))
    body = T.strip_refs(core)
    ui_table = bool(UI_CONTEXT.search(body)) and not DB_CONTEXT.search(body)
    scores: dict[str, float] = {}
    matched: dict[str, list[str]] = {}
    for kind, rules in RULES.items():
        s = 0.0
        for rx, weight in rules:
            if rx is _SCHEMA_PLAIN and ui_table:
                continue
            src = core if rx.pattern.startswith("^") or rx.pattern.startswith(r"\?") else body
            hits = _hits(rx, src)
            if hits:
                s += weight * (1 + 0.5 * (min(len(hits), 3) - 1))
                matched.setdefault(kind, []).extend(h for h in hits
                                                     if h not in matched.get(kind, []))
        if s:
            scores[kind] = s
    for rx, kind, bonus in LEAD_VERB:
        m = rx.search(core)
        if m and not (kind == "bugfix" and "trivial" in scores):  # "fix the typo"
            scores[kind] = scores.get(kind, 0.0) + bonus
            lead = m.group(0).strip().lower()
            terms = [t for t in matched.get(kind, []) if t != lead]
            matched[kind] = [lead] + terms
            break
    # concurrency / leak bugs are hard-to-diagnose bugs, not routine fixes
    hard = _hits(HARD, body)
    extra = _extra_hard(hard_terms or [])
    if extra:
        hard += [h for h in _hits(extra, body) if h not in hard]
    if hard and ("bugfix" in scores or "debugging" in scores):
        scores["debugging"] = scores.get("debugging", 0.0) + 1.5
        matched.setdefault("debugging", []).extend(h for h in hard
                                                   if h not in matched.get("debugging", []))
    # "add/write tests" is a tests task, not a feature that happens to mention tests
    if scores.get("tests", 0) >= 3.0 and "feature" in scores:
        scores["feature"] = max(0.0, scores["feature"] - 2.5)
    _context_rules(core, body, hard, scores, matched)
    # questions that also carry a strong change cue ("can you fix ...?") are change requests
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], _PRIORITY.index(kv[0])))
    if ranked:
        kind, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        strength = min(1.0, top / 4.0)
        margin = (top - second) / top if top else 0.0
        conf = strength * (0.55 + 0.45 * margin)
        secondary = ranked[1][0] if len(ranked) > 1 and ranked[1][1] >= 0.6 * top else None
    else:
        kind, conf, secondary = "feature", 0.15, None
        matched["feature"] = []

    large, small = _hits(SCOPE_LARGE, body), _hits(SCOPE_SMALL, body)
    scope = ("medium" if large and small else "large" if large else "small" if small
             else "unspecified")

    steps = count_steps(core)
    v, vterms = _vagueness(raw, body, located, steps)
    return TaskClass(kind=kind, confidence=round(conf, 3), scores=scores, matched=matched,
                     secondary=secondary, scope=scope, scope_terms=large + small, steps=steps,
                     vagueness=v, vague_terms=vterms, destructive=_hits(DESTRUCTIVE, body),
                     sweeping=_hits(SWEEPING, body), hard=hard,
                     words=len(raw.split()))


def _bump(scores: dict, matched: dict, kind: str, by: float, term: str) -> None:
    scores[kind] = scores.get(kind, 0.0) + by
    if term not in matched.setdefault(kind, []):
        matched[kind].append(term)


def _context_rules(core: str, body: str, hard: list[str], scores: dict, matched: dict) -> None:
    """Score adjustments for words whose meaning depends on the rest of the prompt."""
    typo = _hits(TYPO, body)
    if typo and "trivial" in scores:
        _bump(scores, matched, "trivial", TYPO_BONUS, typo[0])
    rate = _hits(RATE_LIMIT, body)
    if rate:
        on_auth_or_api = bool(AUTH_API.search(core))
        _bump(scores, matched, "security" if on_auth_or_api else "feature",
              4.0 if on_auth_or_api else 1.5, rate[0])
    if WHY_QUESTION.search(core):
        if hard or BUG_WORDS.search(body):
            _bump(scores, matched, "debugging", 3.5, "why")
            scores["question"] = scores.get("question", 0.0) - 3.5
        else:
            # a why-question without a failure: the symptom words are not a bug report
            scores["debugging"] = scores.get("debugging", 0.0) - 3.0
    for kind in [k for k, v in scores.items() if v <= 0]:
        del scores[kind]
        matched.pop(kind, None)


# tie-break order: prefer the cheaper interpretation ("start low")
_PRIORITY = ["question", "trivial", "tests", "bugfix", "feature", "refactor", "perf",
             "migration", "debugging", "design", "security"]


def _vagueness(raw: str, body: str, located: bool | None,
               steps: int = 0) -> tuple[float, list[str]]:
    v = 0.0
    terms = _hits(VAGUE, body)
    # prompt length is deliberately not a signal: a terse prompt can be huge work and a long
    # one can be a trivial edit. Vagueness comes from vague wording and missing specifics.
    v += min(0.45, 0.15 * len(terms))
    ats, paths = T.path_tokens(raw)
    specifics = len(ats) + len(paths) + len(T.identifiers(raw))
    specifics += len(T.QUOTED_RX.findall(raw)) + len(re.findall(r"\b\d+\b", raw)) // 2
    v -= min(0.5, 0.25 * specifics)
    if steps:
        v -= 0.3
    if located is False:
        v += 0.3
    elif located:
        v -= 0.1
    return round(max(0.0, min(1.0, v)), 3), terms

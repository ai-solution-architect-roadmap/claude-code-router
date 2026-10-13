"""Subagent files written by `ccr init` into .claude/agents/.

Each generated file carries a YAML comment inside its frontmatter:
    # ccr:generated sha256=<hash of the file without this line>
The comment never reaches the model (YAML comments are dropped by the frontmatter parser).
On re-init a file is regenerated only when the marker is present and the hash still matches,
i.e. nobody edited it; edited or foreign files are kept unless --force.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

MARK = "# ccr:generated"
_MARK_RX = re.compile(r"^# ccr:generated sha256=([0-9a-f]{16})\b.*$", re.M)
_NAME_RX = re.compile(r"^name:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", re.M)

_ESCALATE = ("If the work turns out bigger or riskier than described (more files, unclear "
             "cause, auth/payments/migrations/infra/secrets, or a design decision), stop and "
             "report what you found so the caller can hand it to a stronger agent.")
_REPORT = ("Finish with a short report: what changed (paths), how you verified it, and "
           "anything left open.")


@dataclass
class AgentSpec:
    name: str
    tier: str | None           # config tier providing model/effort (None = fixed)
    description: str
    tools: str | None          # comma list; None inherits every tool
    body: str
    filename: str = ""
    model: str | None = None   # fixed model when tier is None
    max_turns: int | None = None
    omit_claude_md: bool = False

    @property
    def file(self) -> str:
        return self.filename or f"{self.name}.md"


SPECS = [
    AgentSpec(
        "ccr-quick", "T0",
        "Fast, cheap worker for lookups and trivial edits: where is X defined, what does Y "
        "return, rename, typo, one-line or config tweak. Use proactively when ccr routes a "
        "task to T0.",
        "Read, Grep, Glob, Bash, Edit",
        "You handle quick lookups and trivial edits.\n\n"
        "- Answer directly and briefly; cite file:line for code facts.\n"
        "- Edits: smallest change that does the job; match surrounding style.\n"
        "- Do not refactor, reformat or touch unrelated code.\n"
        f"- {_ESCALATE}",
        max_turns=25),
    AgentSpec(
        "ccr-small", "T1",
        "Small, well-scoped changes: a bug fix with a known cause, a test for one function, "
        "a single-file edit that follows existing patterns. Use proactively when ccr routes "
        "a task to T1.",
        "Read, Grep, Glob, Bash, Edit, Write",
        "You make small, well-scoped code changes.\n\n"
        "1. Read the relevant code and its tests before editing.\n"
        "2. Make the minimal change; follow the file's existing patterns.\n"
        "3. Run the closest tests (or a quick check) and fix what you broke.\n\n"
        f"{_ESCALATE}\n\n{_REPORT}"),
    AgentSpec(
        "ccr-implement", "T2",
        "Implements features, endpoints, refactors and multi-file changes that follow "
        "existing patterns, with tests. Use proactively when ccr routes a task to T2.",
        "Read, Grep, Glob, Bash, Edit, Write, NotebookEdit, WebFetch",
        "You implement features and multi-file changes.\n\n"
        "1. Find the existing patterns (similar features, helpers, tests) and reuse them.\n"
        "2. Plan the change briefly, then implement it end to end.\n"
        "3. Add or update tests; run them and the linters the project uses.\n"
        "4. Keep the diff focused: no drive-by refactors.\n\n"
        "If you hit a design decision with real trade-offs, or the change reaches "
        "auth/payments/migrations/infra/secrets, stop and report so the caller can escalate."
        f"\n\n{_REPORT}"),
    AgentSpec(
        "ccr-architect", "T3",
        "Senior engineer for design, unclear or intermittent bugs, performance, migrations "
        "and security-sensitive work: investigates first, then implements or proposes a "
        "plan. Use proactively when ccr routes a task to T3.",
        None,
        "You handle hard problems: design, unclear root causes, performance, migrations and "
        "security-sensitive changes.\n\n"
        "1. Investigate before concluding: read the code paths involved, form hypotheses and "
        "verify them (tests, logs, small experiments).\n"
        "2. Weigh the options explicitly and pick one, saying why.\n"
        "3. Implement with tests, or return a concrete plan if the caller asked for one.\n"
        "4. Call out risks, migrations and rollback steps.\n\n"
        "Finish with: root cause or decision, what changed, how it was verified, open risks."),
    AgentSpec(
        "ccr-critical", "T4",
        "Most careful worker for critical-path changes: auth, payments, destructive "
        "migrations, infra, secrets, or code marked critical in project memory. Use "
        "proactively when ccr routes a task to T4.",
        None,
        "You change critical code (auth, payments, data migrations, infra, secrets). "
        "Correctness beats speed.\n\n"
        "1. Map every caller and data path the change touches before editing.\n"
        "2. Prefer the smallest reversible change; keep backwards compatibility unless told "
        "otherwise.\n"
        "3. Add tests for the failure modes (bad input, partial failure, retries, "
        "permissions), not only the happy path.\n"
        "4. Never weaken security checks, delete data or rotate secrets without explicit "
        "instruction; flag anything irreversible.\n\n"
        "Finish with: what changed, how it was verified, risks, and rollback steps."),
    AgentSpec(
        "Explore", None,
        "Fast read-only codebase search on haiku: find files, symbols, usages and config, and "
        "answer where/how-is-X questions. Say how thorough to be (quick, medium, very "
        "thorough). Use proactively for any search needing more than a couple of greps.",
        "Read, Grep, Glob",
        "You are a read-only search agent. Never modify files.\n\n"
        "- Start broad with Glob/Grep, then read only the excerpts you need.\n"
        "- Match the requested thoroughness; stop once the question is answered.\n"
        "- Report conclusions with file:line references, not file dumps.",
        filename="Explore.md", model="haiku", omit_claude_md=True),
]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _strip_marker(text: str) -> str:
    text = text.replace("\r\n", "\n")
    return "\n".join(line for line in text.split("\n") if not line.startswith(MARK))


def render(spec: AgentSpec, cfg) -> str:
    if spec.tier is not None:
        t = cfg.tier(spec.tier)
        model, effort = t.model, t.effort
    else:
        model, effort = spec.model, None
    fm = [f"name: {spec.name}", f"description: {json.dumps(spec.description)}"]
    if spec.tools:
        fm.append(f"tools: {spec.tools}")
    fm.append(f"model: {model}")
    if effort:
        fm.append(f"effort: {effort}")
    if spec.max_turns:
        fm.append(f"maxTurns: {spec.max_turns}")
    if spec.omit_claude_md:
        fm.append("omitClaudeMd: true")
    body = "---\n" + "\n".join(fm) + "\n---\n\n" + spec.body.rstrip() + "\n"
    mark = (f"{MARK} sha256={_hash(body)} -- edit freely; `ccr init` leaves edited files "
            f"alone (--force regenerates)")
    return body.replace("---\n", "---\n" + mark + "\n", 1)


def state(text: str) -> str:
    """"clean" (ours, unedited) | "edited" (ours, changed since generation) | "foreign"."""
    m = _MARK_RX.search(text)
    if not m:
        return "foreign"
    return "clean" if _hash(_strip_marker(text)) == m.group(1) else "edited"


def declared_name(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    m = _NAME_RX.search(text[: end if end > 0 else len(text)])
    return m.group(1).strip() if m else None


def names_in(agents_dir: Path) -> dict[str, list[Path]]:
    """{declared agent name (lowercase): [files]} for existing .claude/agents/*.md."""
    out: dict[str, list[Path]] = {}
    if not agents_dir.is_dir():
        return out
    for p in sorted(agents_dir.glob("*.md")):
        try:
            n = declared_name(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if n:
            out.setdefault(n.lower(), []).append(p)
    return out

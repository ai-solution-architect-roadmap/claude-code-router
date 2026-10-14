"""`ccr run`: route a task, run it with the user's own `claude -p`, escalate once on failure.

Each attempt runs
  claude -p --model <tier model> --effort <tier effort> --output-format json
         --max-turns N [--max-budget-usd X] [--permission-mode M]
with the task on stdin (never on the command line) and CCR_INTERNAL=1 so ccr's own hooks
stay silent inside it. The result object of the installed build (verified on 2.1.x) has
`type: "result"`, `subtype` (`success`, `error_max_turns`, `error_max_budget_usd`,
`error_during_execution`, ...), `is_error`, `num_turns`, `total_cost_usd`, `usage`
(input/output/cache tokens), `modelUsage` ({model id: {inputTokens, outputTokens,
cacheReadInputTokens, cacheCreationInputTokens, costUSD}}), `session_id` and `result` (the
final text; absent on error subtypes). A failing run exits 1 and still prints that object.

Success = exit 0, `is_error` false, `subtype` success and, when configured, `run.test_command`
exiting 0. On failure the next attempt runs exactly one tier higher (never skipping a tier;
T4 only when a critical-path rule allows it) until `run.max_escalations` (default 1) is used
up. A failure that escalation cannot fix (claude missing, unparseable output, timeout)
stops the run with outcome `error`, which learning does not count. Every attempt appends one
history record (whitelisted fields only, no prompt text); the escalated attempt is recorded
as a `failure` of its tier with `escalated_to`, the next one carries `escalated_from`.
`max_budget_usd` caps the whole run: later attempts only get what is left.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ccr.config import TIERS, Config

if TYPE_CHECKING:
    from ccr.route import Decision

TEST_TAIL_CHARS = 3000


@dataclass
class Proc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


# runner(argv, stdin_text, cwd, env, timeout) -> Proc; raises OSError /
# subprocess.TimeoutExpired like subprocess.run
Runner = Callable[[list, str, Path, dict, float], Proc]
TestRunner = Callable[[str, Path, dict, float], Proc]


def default_runner(argv: list, stdin_text: str, cwd: Path, env: dict, timeout: float) -> Proc:
    r = subprocess.run(argv, input=stdin_text, capture_output=True, text=True, cwd=cwd,
                       env=env, timeout=timeout, check=False)
    return Proc(r.returncode, r.stdout or "", r.stderr or "")


def default_test_runner(command: str, cwd: Path, env: dict, timeout: float) -> Proc:
    r = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd, env=env,
                       timeout=timeout, check=False)
    return Proc(r.returncode, r.stdout or "", r.stderr or "")


@dataclass
class Attempt:
    tier: str
    model: str
    effort: str
    outcome: str = "failure"          # success | failure | error
    reason: str = ""                  # success, error_max_turns, exit_1, test_failed, ...
    subtype: str | None = None
    exit_code: int | None = None
    is_error: bool | None = None
    turns: int | None = None
    cost_usd: float | None = None
    tokens: int = 0
    usage: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)
    session: str | None = None
    duration_ms: int | None = None
    test: str | None = None           # pass | fail | timeout | None (not configured)
    result: str = ""                  # final text (printed, never stored)
    test_tail: str = ""               # end of the test output (fed to the next attempt only)
    escalated_to: str | None = None
    record: dict = field(default_factory=dict)


@dataclass
class RunResult:
    decision: object
    attempts: list[Attempt]
    run_id: str
    stopped: str = ""                 # why escalation stopped, if it did

    @property
    def success(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].outcome == "success"

    @property
    def cost_usd(self) -> float:
        return round(sum(a.cost_usd or 0.0 for a in self.attempts), 6)

    @property
    def tokens(self) -> int:
        return sum(a.tokens for a in self.attempts)

    def to_dict(self) -> dict:
        d = self.decision
        return {"run_id": self.run_id, "success": self.success, "stopped": self.stopped,
                "class": d.task_class.kind, "area": d.area, "routed_tier": d.tier.name,
                "cost_usd": self.cost_usd, "tokens": self.tokens,
                "attempts": [{k: v for k, v in a.__dict__.items()
                              if k not in ("record", "test_tail")} for a in self.attempts]}


def find_claude(env: dict) -> str | None:
    return shutil.which("claude", path=env.get("PATH"))


def build_argv(exe: str, tier, *, max_turns: int, max_budget_usd: float | None,
               permission_mode: str | None) -> list[str]:
    argv = [exe, "-p", "--model", tier.model, "--effort", tier.effort,
            "--output-format", "json", "--max-turns", str(int(max_turns))]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{max_budget_usd:.4f}".rstrip("0").rstrip(".")]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    return argv


def _int(v) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def parse_result(stdout: str) -> dict | None:
    """The `type: result` object from --output-format json (last JSON line wins)."""
    text = (stdout or "").strip()
    if not text:
        return None
    cands = [text] + [ln for ln in reversed(text.splitlines()) if ln.strip().startswith("{")]
    for c in cands:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, list):     # stream-style array: take the result element
            obj = next((o for o in reversed(obj) if isinstance(o, dict)
                        and o.get("type") == "result"), None)
        if isinstance(obj, dict) and ("subtype" in obj or "is_error" in obj):
            return obj
    return None


def absorb_result(a: Attempt, data: dict) -> None:
    a.subtype = data.get("subtype") if isinstance(data.get("subtype"), str) else None
    a.is_error = data.get("is_error") if isinstance(data.get("is_error"), bool) else None
    a.turns = data.get("num_turns") if isinstance(data.get("num_turns"), int) else None
    c = data.get("total_cost_usd")
    a.cost_usd = round(float(c), 6) if isinstance(c, (int, float)) and \
        not isinstance(c, bool) else None
    d = data.get("duration_ms")
    a.duration_ms = int(d) if isinstance(d, (int, float)) and not isinstance(d, bool) else None
    a.session = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    a.usage = {k: _int(u.get(f"{k}_tokens")) for k in
               ("input", "output", "cache_creation_input", "cache_read_input")}
    a.usage = {k.replace("_input", ""): v for k, v in a.usage.items() if v}
    models = {}
    mu = data.get("modelUsage") if isinstance(data.get("modelUsage"), dict) else {}
    for mid, m in mu.items():
        if not isinstance(m, dict):
            continue
        toks = sum(_int(m.get(k)) for k in ("inputTokens", "outputTokens",
                                             "cacheCreationInputTokens"))
        cost = m.get("costUSD")
        models[str(mid)[:80]] = {"tokens": toks, **(
            {"cost_usd": round(float(cost), 6)} if isinstance(cost, (int, float)) else {})}
    a.models = models
    # same definition as hook records: input + output + cache creation (no cache reads)
    a.tokens = sum(v["tokens"] for v in models.values()) if models else \
        sum(a.usage.get(k, 0) for k in ("input", "output", "cache_creation"))
    r = data.get("result")
    a.result = r if isinstance(r, str) else ""


def _escalation_note(prev: Attempt) -> str:
    why = {"test_failed": "the test command failed afterwards",
           "error_max_turns": "it ran out of turns",
           "error_max_budget_usd": "it hit the budget cap"}.get(prev.reason, prev.reason)
    note = (f"\n\n[ccr] A previous attempt at {prev.tier} ({prev.model}/{prev.effort}) did "
            f"not finish the task: {why}. The working tree may contain its partial changes; "
            f"review them, then complete the task.")
    if prev.test_tail:
        note += f"\nEnd of the test output:\n```\n{prev.test_tail}\n```"
    return note


def next_tier(cur: int, decision, cfg) -> tuple[int | None, str]:
    """(next tier index or None, reason when None). Exactly one tier up, never skipping."""
    if cur >= 4:
        return None, "already at T4"
    nxt = cur + 1
    rule = bool(getattr(decision, "t4_rules", None))
    ceiling = TIERS.index(cfg["routing"]["ceiling_without_critical"])
    if nxt == 4 and not rule:
        return None, "T4 needs a critical-path rule"
    if nxt > ceiling and not rule:
        return None, f"routing.ceiling_without_critical is {TIERS[ceiling]}"
    return nxt, ""


def _invoke(a: Attempt, exe: str | None, t, runner: Runner, prompt: str, root: Path, env: dict,
            rc: dict, max_turns: int, left: float | None, permission_mode: str | None) -> None:
    """Run claude once and fill `a` with the outcome."""
    if exe is None:
        a.outcome, a.reason = "error", "claude_not_found"
        return
    argv = build_argv(exe, t, max_turns=max_turns, max_budget_usd=left,
                      permission_mode=permission_mode)
    try:
        p = runner(argv, prompt, root, env, float(rc["timeout_s"]))
    except subprocess.TimeoutExpired:
        a.outcome, a.reason = "error", "timeout"
        return
    except OSError as e:
        a.outcome, a.reason = "error", f"cannot_run ({e.__class__.__name__})"
        return
    a.exit_code = p.returncode
    data = parse_result(p.stdout)
    if data is None:
        a.outcome, a.reason = "error", f"no_json_exit_{p.returncode}"
        return
    absorb_result(a, data)
    if p.returncode == 0 and a.is_error is False and a.subtype == "success":
        a.outcome, a.reason = "success", "success"
    else:
        a.outcome = "failure"
        a.reason = (a.subtype if a.subtype and a.subtype != "success"
                    else "is_error" if a.is_error else f"exit_{p.returncode}")


def _run_tests(a: Attempt, test_cmd: str, test_runner: TestRunner, root: Path, env: dict,
               rc: dict) -> None:
    """Run the project's tests after a successful attempt; a failure downgrades the outcome."""
    try:
        tp = test_runner(test_cmd, root, {k: v for k, v in env.items() if k != "CCR_INTERNAL"},
                         float(rc["test_timeout_s"]))
        a.test = "pass" if tp.returncode == 0 else "fail"
        a.test_tail = ((tp.stdout or "") + (tp.stderr or ""))[-TEST_TAIL_CHARS:]
    except subprocess.TimeoutExpired:
        a.test = "timeout"
    except OSError:
        a.test = "fail"
    if a.test != "pass":
        a.outcome, a.reason = "failure", "test_failed"


def run_task(task: str, root: str | Path, *, cfg: Config | None = None, tier: str | None = None,
             max_turns: int | None = None, max_budget_usd: float | None = None,
             max_escalations: int | None = None, test_command: str | None = None,
             use_tests: bool = True, permission_mode: str | None = None,
             allow_llm: bool = True, runner: Runner | None = None,
             test_runner: TestRunner | None = None, environ: dict | None = None,
             decision: Decision | None = None, echo: Callable[[str], None] | None = None,
             record: bool = True, claude_exe: str | None = None) -> RunResult:
    from ccr import history as H
    from ccr.config import load_config
    root = Path(root).resolve()
    cfg = cfg or load_config(root)
    rc = cfg["run"]
    runner = runner or default_runner
    test_runner = test_runner or default_test_runner
    say = echo or (lambda s: None)
    env = dict(os.environ if environ is None else environ)
    env["CCR_INTERNAL"] = "1"                 # ccr hooks no-op inside the child
    env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)  # it would override --effort
    max_turns = int(max_turns or rc["max_turns"])
    budget = max_budget_usd if max_budget_usd is not None else rc["max_budget_usd"]
    max_esc = int(rc["max_escalations"] if max_escalations is None else max_escalations)
    test_cmd = (test_command or rc["test_command"]) if use_tests else None
    permission_mode = permission_mode or rc["permission_mode"]

    if decision is None:
        from ccr.route import decide
        decision = decide(task, root, cfg=cfg, allow_llm=allow_llm, llm_runner=None)
    d = decision
    assist_cost = None
    if d.llm_used and "[~$" in d.llm:
        with contextlib.suppress(ValueError):
            assist_cost = float(d.llm.rsplit("[~$", 1)[1].rstrip("]"))
    cur = TIERS.index(tier) if tier else d.tier.index
    res = RunResult(decision=d, attempts=[], run_id=H.new_id())
    exe = claude_exe or find_claude(env)
    escalations = 0
    prompt = task
    while True:
        t = cfg.tier(cur)
        a = Attempt(tier=t.name, model=t.model, effort=t.effort)
        left = None if budget is None else round(float(budget) - res.cost_usd, 4)
        prev = res.attempts[-1] if res.attempts else None
        say(f"attempt {len(res.attempts) + 1}: {t.name} {t.model}/{t.effort}"
            + (f" (escalated from {prev.tier})" if prev else "")
            + (f", budget left ${left:.2f}" if left is not None else ""))
        t0 = time.time()
        _invoke(a, exe, t, runner, prompt, root, env, rc, max_turns, left, permission_mode)
        if a.duration_ms is None:
            a.duration_ms = int((time.time() - t0) * 1000)
        if a.outcome == "success" and test_cmd:
            say(f"  test: {test_cmd}")
            _run_tests(a, test_cmd, test_runner, root, env, rc)
        res.attempts.append(a)

        nxt, why = (None, "")
        if a.outcome == "failure":
            if escalations >= max_esc:
                why = f"max_escalations {max_esc} reached"
            else:
                nxt, why = next_tier(cur, d, cfg)
            if nxt is not None and budget is not None and \
                    float(budget) - res.cost_usd < 0.001:
                nxt, why = None, f"budget ${float(budget):.2f} used up (${res.cost_usd:.4f})"
            if nxt is not None:
                a.escalated_to = TIERS[nxt]
        if record:
            rec = H.make_record(d, a.outcome, tier=a.tier, model=a.model, effort=a.effort,
                                source="run", run_id=res.run_id, attempt=len(res.attempts),
                                reason=a.reason, subtype=a.subtype, exit_code=a.exit_code,
                                tokens=a.tokens or None, cost_usd=a.cost_usd, turns=a.turns,
                                usage=a.usage or None, models=a.models or None,
                                duration_ms=a.duration_ms, test=a.test,
                                session=a.session, escalated_to=a.escalated_to,
                                escalated_from=prev.tier if prev else None,
                                forced=True if tier else None,
                                assist_cost_usd=assist_cost if not prev else None)
            a.record = rec
            try:
                H.append(root, rec)
            except OSError as e:
                say(f"  ! could not write history: {e}")
        cost = f"${a.cost_usd:.4f}" if a.cost_usd is not None else "cost n/a"
        say(f"  -> {a.outcome} ({a.reason}; {a.turns if a.turns is not None else '?'} turns, "
            f"{a.tokens} tokens, {cost}" + (f", test {a.test}" if a.test else "") + ")")
        if a.outcome != "failure":
            break
        if nxt is None:
            res.stopped = why
            say(f"  no escalation: {why}")
            break
        say(f"  escalating {a.tier} -> {TIERS[nxt]}")
        escalations += 1
        prompt = task + _escalation_note(a)
        cur = nxt
    return res

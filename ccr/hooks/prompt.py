"""UserPromptSubmit: route the prompt with decide() (rules only, never an LLM) and inject one
`ccr:` line of context, e.g.

  ccr: T2 (sonnet/medium) — touches 6 files in billing/, 14 dependents, tests present.
  Delegate implementation to ccr-implement; use Explore (haiku) for search.

Silent for: `!noroute` anywhere in the prompt (a leading `!` is bash mode in the interactive
CLI, so a trailing `!noroute` is the practical form), slash commands, synthetic user turns
such as `<task-notification>` (a background subagent finishing), short chatter
("thanks", "yes go on"), `hooks.route_prompts: false`, a corrupt graph or config.

Prompt linting (`lint.mode`, ccr/evaluate/lint.py): a vague prompt also gets one hint line
for the user, e.g. "No file named. Did you mean `app/config.py`?". The hint never blocks
unless `lint.mode: strict`, and the task record notes it (`lint`) so its effect on turns and
failures can be measured. `!nolint` skips the linter for one prompt.
"""
from __future__ import annotations

import os
import posixpath
import re
import time

from ccr.hooks.common import (EFFORT_RANK, Msg, effort_level, load_session, log, main_model,
                              model_alias, model_rank, save_session)
from ccr.hooks.tasks import finalize, infer_outcome, is_retry, start

NOROUTE = re.compile(r"(?:^|\s)!noroute\b", re.I)
NOLINT = re.compile(r"(?:^|\s)!nolint\b", re.I)
# Messages Claude Code injects as user turns (background task results, slash command
# echoes, bash-mode output): `<task-notification>`, `<command-name>`, `<bash-stdout>`, ...
SYNTHETIC = re.compile(r"^\s*<(?:task-notification|system-reminder|[a-z]+(?:-[a-z]+)+)[\s>]")
MAX_EVAL_CHARS = 8000
CHANGE_KINDS = {"trivial", "tests", "bugfix", "feature", "refactor", "perf", "migration",
                "security"}


CHATTER_MAX_WORDS = 4
RETRY_REUSE_WORDS = 8         # a short retry ("still failing") keeps the previous facts
CHATTER_MAX_CONFIDENCE = 0.3


def is_chatter(prompt: str, d) -> bool:
    """Short follow-ups with nothing to route on ("thanks", "yes go ahead", "fix it")."""
    if len(prompt.split()) > CHATTER_MAX_WORDS or d.location.candidates or \
            d.risk.critical_terms:
        return False
    return (d.task_class.kind in ("question", "trivial")
            or d.confidence < CHATTER_MAX_CONFIDENCE)


def facts(d) -> str:
    prim = d.location.primary()
    parts = []
    if prim:
        paths = [c.path for c in prim]
        if len(paths) <= 2:
            parts.append("touches " + " and ".join(paths))
        else:
            dirs = [posixpath.dirname(p) for p in paths]
            common = posixpath.commonpath(dirs) if all(dirs) else ""
            parts.append(f"touches {len(paths)} files" + (f" in {common}/" if common else ""))
    else:
        parts.append("no specific files located")
    r = d.risk
    if r.dependents:
        parts.append(f"{r.dependents} dependent{'s' if r.dependents != 1 else ''}")
    if r.tests == "direct":
        parts.append("tests present")
    elif r.tests == "name-matched":
        parts.append("only name-matched tests")
    elif r.tests == "none":
        parts.append("no tests found")
    if r.critical_groups:
        parts.append("critical path: " + "/".join(r.critical_groups))
    kinds = sorted({f.kind for f in r.memory_flags})
    if kinds:
        parts.append("memory flags: " + "/".join(kinds))
    if r.level == "high":
        parts.append(f"risk high ({r.score:.2f})")
    return ", ".join(parts)


def ui_line(d, tier, kind: str, fact: str, retry: bool) -> str:
    """Short user-facing summary, e.g. `ccr → T2 sonnet/medium · ccr-implement · 2 files, risk high`."""
    prim = d.location.primary()
    bits = []
    if prim:
        bits.append(f"{len(prim)} file{'s' if len(prim) != 1 else ''}")
    if d.risk.level != "low":
        bits.append(f"risk {d.risk.level}")
    if retry:
        bits.append("retry")
    tail = f" · {', '.join(bits)}" if bits else f" · {kind}"
    return f"ccr → {tier.name} {tier.model}/{tier.effort} · {tier.agent}{tail}"


def iterative_reason(d, retry: bool) -> str | None:
    tc = d.task_class
    if retry:
        return "a retry"
    if tc.kind == "debugging":
        return "debugging"
    if tc.vague:
        return "a vague request"
    if tc.kind == "question" and tc.scope == "large":
        return "a broad design question"
    return None


def advice(d, tier, main: str | None, main_effort: str | None, why_iter: str | None,
           cfg) -> str:
    kind = d.task_class.kind
    mr, tr = model_rank(main), model_rank(tier.model)
    eff_ok = EFFORT_RANK.get(main_effort or "", -1) >= EFFORT_RANK.get(tier.effort, 0)
    same = mr is not None and mr == tr and eff_ok
    search = "use Explore (haiku) for search."
    if tier.name == "T0" and kind == "question":
        out = f"Answer directly; {search}"
    elif same:
        out = (f"Main session already runs {model_alias(main)}/{main_effort}: handle it "
               f"directly; {search}")
    else:
        verb = ("Delegate implementation to" if kind in CHANGE_KINDS else
                "Delegate the investigation to" if kind == "debugging" else "Delegate to")
        out = f"{verb} {tier.agent}; {search}"
        if tier.name == "T4":
            out += " Review its diff before accepting."
    if why_iter and mr is not None and tr is not None and mr < tr and \
            cfg["hooks"]["model_advice"]:
        out += (f" This looks iterative ({why_iter}) and the main model is "
                f"{model_alias(main)}: consider `/model {model_alias(tier.model) or tier.model}`"
                f" for this conversation, then `/model {model_alias(main)}` afterwards.")
    return out


def _retry(d, cfg, pending: dict, session: dict, prompt: str, kind: str, area: str, fact: str):
    """A retry escalates one tier above the failed attempt (within the ceiling)."""
    from ccr.config import TIERS, TOP
    prev = pending.get("tier")
    p = TIERS.index(prev) if prev in TIERS else 0
    ceiling = TOP if (d.t4_rules or d.risk.critical) else \
        TIERS.index(cfg["routing"]["ceiling_without_critical"])
    own = 0 if d.reclassed else d.tier.index    # "still failing" names no code: not debugging
    new = max(own, min(p + 1, ceiling))
    tier = cfg.tier(new)
    if pending.get("class"):
        kind = pending["class"]
    if pending.get("area") and area == "*":
        area = pending["area"]
    if session.get("facts") and (not d.location.candidates
                                 or len(prompt.split()) <= RETRY_REUSE_WORDS):
        fact = session["facts"]
    note = (f"Previous attempt ({prev}) did not work: escalated to {tier.name}."
            if new > p else f"Previous attempt ({prev}) did not work and it is "
                            f"already the ceiling: re-plan before retrying.")
    return tier, prev, kind, area, fact, [note]


REWORK = re.compile(
    r"\b(?:not\s+quite|not\s+what\s+i|i\s+meant|i\s+said|instead|rather\s+than|actually|"
    r"wrong\s+(?:file|function|place|approach)|you\s+(?:missed|forgot|broke|removed|changed)|"
    r"too\s+(?:shallow|simple|basic|vague)|redo|do\s+it\s+properly|start\s+again|"
    r"that'?s\s+not\s+(?:it|right|what))\b", re.I)


def escalation_signals(prompt: str, pending: dict, session: dict, d, main, cfg,
                       now: float | None = None) -> list[str]:
    """Signs that the previous task's answer fell short without a "still failing" phrase:
      manual_model_up  you moved the main session to a stronger model before this prompt, and
                       this prompt is about the same code or leans on the last answer
      possible_rework  a dissatisfied reply ("not quite", "I meant", "you missed") about the same
                       code within hooks.rework_minutes of the last activity"""
    paths = {c.path for c in d.location.candidates}
    same = bool(paths & set(session.get("files") or ())) or (
        d.area not in ("*", "") and d.area == pending.get("area"))
    sig = []
    old, new = model_rank(pending.get("main_model")), model_rank(main)
    if old is not None and new is not None and new > old and (same or is_followup(prompt, d)):
        sig.append("manual_model_up")
    task = session.get("task") if isinstance(session.get("task"), dict) else {}
    last = float(task.get("last_ts") or pending.get("ts") or 0)
    window = int(cfg["hooks"]["rework_minutes"]) * 60
    if same and REWORK.search(prompt[:300]) and last and (now or time.time()) - last <= window:
        sig.append("possible_rework")
    return sig


def _record(root, sid, tp, session: dict, pending, d, tier, kind: str, area: str, fact: str,
            retry: bool, escalated_from, main, outcome: str | None, prompt: str = "",
            cfg=None, lint_rec: dict | None = None) -> None:
    """Close the previous task and start tracking this one in the session file."""
    from ccr.config import TIERS
    if tp:
        session["transcript"] = tp
        if "offset" not in session:   # first task here: earlier usage is not ours
            try:
                session["offset"] = os.path.getsize(tp)
            except OSError:
                session["offset"] = 0
    if pending is not None and cfg is not None and not retry:
        sig = escalation_signals(prompt, pending, session, d, main, cfg)
        if sig:
            task = session.setdefault("task", {})
            task["signals"] = sorted(set(task.get("signals") or []) | set(sig))
    if pending is not None:          # absorbs the transcript tail, then records it
        if retry and escalated_from in TIERS and tier.index > TIERS.index(escalated_from):
            pending["escalated_to"] = tier.name
        finalize(root, sid, session, outcome)
    start(session, {
        "ts": int(time.time()), "class": kind, "area": area, "tier": tier.name,
        "model": tier.model, "effort": tier.effort, "risk": d.risk.score,
        "confidence": d.confidence, "source": "hook", "escalated_from": escalated_from,
        "main_model": model_alias(main), "trace": d.trace(),
        **({"lint": lint_rec} if lint_rec else {})})
    session["facts"] = fact
    if d.location.candidates:
        session["files"] = [c.path for c in d.location.candidates[:3]]
    save_session(root, sid, session)


ANAPHORA = re.compile(r"\b(?:that|it|this|those|them|these|the\s+same|same\s+(?:for|here|thing|way)|"
                      r"too|also|as\s+well|again|the\s+other|next|another\s+one)\b", re.I)
FOLLOWUP_MAX_WORDS = 6
FOLLOWUP_MAX_CONFIDENCE = 0.35


def is_followup(prompt: str, d) -> bool:
    """A reply that leans on the conversation ("do the same for refunds", "make it stricter",
    "the header row is wrong") rather than carrying its own anchor in the code."""
    words = len(prompt.split())
    return bool(ANAPHORA.search(prompt)) and words <= 12 or words <= FOLLOWUP_MAX_WORDS \
        and not d.location.located


def _follow_up(prompt: str, d, pending: dict, session: dict, root, cfg, graph_kw: dict):
    """Give a follow-up the previous task's context: its files for locating, and (when the
    follow-up itself says little about the kind of work) its class and tier."""
    from ccr.route import decide
    note = None
    files = [f for f in session.get("files") or [] if isinstance(f, str)]
    if files and not d.location.located:
        d = decide(prompt[:MAX_EVAL_CHARS] + "\n" + " ".join(files), root, cfg=cfg,
                   allow_predict=False, **graph_kw)     # one Haiku wait per prompt at most
        note = "uses the files from the previous task"
    inherit = None
    if d.task_class.confidence < FOLLOWUP_MAX_CONFIDENCE and pending.get("tier") \
            and pending.get("class") in CHANGE_KINDS:
        inherit = pending
        note = f"continues the previous {pending['class']} task at {pending['tier']}"
    return d, inherit, note


def _decide(prompt, root, cfg, g, tp, session, pending, retry):
    """decide() for the prompt, plus the previous task's context for a follow-up."""
    from ccr.route import decide
    inherit = note = None
    try:
        d = decide(prompt[:MAX_EVAL_CHARS], root, cfg=cfg, graph=g, transcript_path=tp)
        if pending is not None and not retry and is_followup(prompt, d) \
                and not is_chatter(prompt, d):
            d, inherit, note = _follow_up(prompt, d, pending, session, root, cfg,
                                          {"graph": g, "transcript_path": tp})
    finally:
        if g is not None:
            g.close()
    return d, inherit, note


def _prompt_key(prompt: str) -> str:
    import hashlib
    return hashlib.sha256(" ".join(prompt.split()).lower().encode()).hexdigest()[:16]


def prompt_lint(prompt: str, d, root, cfg, session: dict, pending, retry: bool,
                sid: str | None = None):
    """(Lint or None, block reason or None) for this prompt. Retries and follow-ups that lean
    on the conversation are never linted: "fix it" after a discussion is clear enough.
    Strict mode stops a vague prompt once; the same prompt sent again goes through."""
    from ccr.evaluate import lint as L
    m = L.mode(cfg)
    if m == "off" or retry or NOLINT.search(prompt) or \
            (pending is not None and is_followup(prompt, d)):
        return None, None
    try:
        found = L.lint(prompt, d, cfg=cfg, root=root)
    except Exception as e:  # noqa: BLE001   a hint is never worth a lost routing line
        log(root, f"prompt lint failed: {e.__class__.__name__}: {e}")
        return None, None
    if found is None or m != "strict" or not sid:   # no session: cannot let a resend through
        return found, None
    key = _prompt_key(prompt)
    if session.get("lint_blocked") == key:
        session.pop("lint_blocked", None)
        return found, None
    session["lint_blocked"] = key
    return found, (f"ccr lint (strict): {found.hint} Add the detail and send again, or send "
                   "the same prompt again (or add !nolint) to go ahead as it is.")


def _lint_step(prompt, d, root, cfg, sid, session: dict, pending, retry: bool):
    """(Lint or None, early result or None). The early result ends the hook: a strict-mode
    block, a hint alone for chatter that opens a session ("fix it"), or "" for chatter that
    continues the current task ("thanks", "yes go on")."""
    found, block = prompt_lint(prompt, d, root, cfg, session, pending, retry, sid)
    if block:
        try:
            save_session(root, sid, session)   # remembers the stopped prompt; task untouched
        except Exception as e:  # noqa: BLE001
            log(root, f"prompt lint state not saved: {e.__class__.__name__}: {e}")
        return found, Msg("", block=block)
    if not retry and is_chatter(prompt, d):
        if found and pending is None:
            return found, Msg("", hint=found.hint)
        return found, ""
    return found, None


def run(data: dict, root, cfg) -> str | None:
    if not cfg["hooks"]["route_prompts"]:
        return None
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    if NOROUTE.search(prompt) or prompt.lstrip().startswith("/") or SYNTHETIC.match(prompt):
        return None                      # also leaves the pending task untouched
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    tp = data.get("transcript_path") if isinstance(data.get("transcript_path"), str) else None

    from ccr.graph.db import db_path
    from ccr.graph.query import Graph
    g = Graph.open(root)
    if g is None and db_path(root).exists():
        return None                      # unreadable graph: fail open, doctor reports it
    session = load_session(root, sid)
    pending = session.get("pending") if isinstance(session.get("pending"), dict) else None
    retry = pending is not None and is_retry(prompt)
    d, inherit, follow_note = _decide(prompt, root, cfg, g, tp, session, pending, retry)
    found, early = _lint_step(prompt, d, root, cfg, sid, session, pending, retry)
    if early is not None:
        return early or None
    hint = found.hint if found else None

    kind, area, fact = d.task_class.kind, d.area, facts(d)
    tier, escalated_from, notes = d.tier, None, []
    if inherit is not None:
        tier, kind = cfg.tier(inherit["tier"]), inherit["class"]
        if inherit.get("area") and area == "*":
            area = inherit["area"]
        fact = session.get("facts") or fact
    if follow_note:
        notes.append(f"Follow-up: {follow_note}.")
    if retry:
        tier, escalated_from, kind, area, fact, notes = _retry(
            d, cfg, pending, session, prompt, kind, area, fact)
    if d.budget.pressure and not retry:
        notes.append(f"Weekly usage {d.budget.used_pct:.0f}%: tier lowered to save quota.")
    if d.confidence < float(cfg["llm"]["threshold"]) and not retry and tier.index < len(cfg.tiers) - 1:
        notes.append("Low routing confidence: escalate one tier if this falls short.")

    main, main_effort = main_model(root, sid, session)
    main_effort = (main_effort or effort_level(data.get("effort"))
                   or effort_level(os.environ.get("CLAUDE_EFFORT")))
    text = (f"ccr: {tier.name} ({tier.model}/{tier.effort}) — {fact}. "
            + advice(d, tier, main, main_effort, iterative_reason(d, retry), cfg))
    if notes:
        text += " " + " ".join(notes)

    try:   # bookkeeping is best effort: the routing line is still worth emitting
        outcome = infer_outcome(prompt, session, cfg) if pending is not None else None
        lint_rec = {**found.record(), "mode": cfg["lint"]["mode"]} if found else None
        _record(root, sid, tp, session, pending, d, tier, kind, area, fact, retry,
                escalated_from, main, outcome, prompt, cfg, lint_rec)
        if cfg["hooks"]["store_prompts"] and isinstance(session.get("pending"), dict):
            from ccr.history import prompts
            prompts.append(root, prompts.snapshot(
                session["pending"]["id"], prompt, d, tier, fact, notes, retry, escalated_from,
                cfg["hooks"]["redact_prompts"]))
    except Exception as e:  # noqa: BLE001
        log(root, f"prompt bookkeeping failed: {e.__class__.__name__}: {e}")
    return Msg(text, ui_line(d, tier, kind, fact, retry), hint=hint)

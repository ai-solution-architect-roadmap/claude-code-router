"""UserPromptSubmit: route the prompt with decide() (rules only, never an LLM) and inject one
`ccr:` line of context, e.g.

  ccr: T2 (sonnet/medium) — touches 6 files in billing/, 14 dependents, tests present.
  Delegate implementation to ccr-implement; use Explore (haiku) for search.

Silent for: `!noroute` anywhere in the prompt (a leading `!` is bash mode in the interactive
CLI, so a trailing `!noroute` is the practical form), slash commands, synthetic user turns
such as `<task-notification>` (a background subagent finishing), short chatter
("thanks", "yes go on"), `hooks.route_prompts: false`, a corrupt graph or config.
"""
from __future__ import annotations

import os
import posixpath
import re
import time

from ccr.hooks.common import (EFFORT_RANK, Msg, effort_level, load_session, log, main_model,
                              model_alias, model_rank, save_session)
from ccr.hooks.tasks import finalize, is_retry, start

NOROUTE = re.compile(r"(?:^|\s)!noroute\b", re.I)
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
    from ccr.config import TIERS
    from ccr.route import decide
    try:
        d = decide(prompt[:MAX_EVAL_CHARS], root, cfg=cfg, graph=g, transcript_path=tp)
    finally:
        if g is not None:
            g.close()

    session = load_session(root, sid)
    pending = session.get("pending") if isinstance(session.get("pending"), dict) else None
    retry = pending is not None and is_retry(prompt)
    if not retry and is_chatter(prompt, d):
        return None                      # "thanks", "yes go on": the current task continues

    tier, escalated_from, notes = d.tier, None, []
    kind, area, fact = d.task_class.kind, d.area, facts(d)
    if retry:
        prev = pending.get("tier")
        p = TIERS.index(prev) if prev in TIERS else 0
        ceiling = 4 if (d.t4_rules or d.risk.critical) else \
            TIERS.index(cfg["routing"]["ceiling_without_critical"])
        new = max(d.tier.index, min(p + 1, ceiling))
        tier = cfg.tier(new)
        escalated_from = prev
        if pending.get("class"):
            kind = pending["class"]
        if pending.get("area") and area == "*":
            area = pending["area"]
        if session.get("facts") and (not d.location.candidates
                                     or len(prompt.split()) <= RETRY_REUSE_WORDS):
            fact = session["facts"]
        notes.append(f"Previous attempt ({prev}) did not work: escalated to {tier.name}."
                     if new > p else f"Previous attempt ({prev}) did not work and it is "
                                     f"already the ceiling: re-plan before retrying.")
    if d.budget.pressure and not retry:
        notes.append(f"Weekly usage {d.budget.used_pct:.0f}%: tier lowered to save quota.")
    if d.confidence < float(cfg["llm"]["threshold"]) and not retry and tier.index < 4:
        notes.append("Low routing confidence: escalate one tier if this falls short.")

    main, main_effort = main_model(root, sid, session)
    main_effort = (main_effort or effort_level(data.get("effort"))
                   or effort_level(os.environ.get("CLAUDE_EFFORT")))
    text = (f"ccr: {tier.name} ({tier.model}/{tier.effort}) — {fact}. "
            + advice(d, tier, main, main_effort, iterative_reason(d, retry), cfg))
    if notes:
        text += " " + " ".join(notes)

    try:   # bookkeeping is best effort: the routing line is still worth emitting
        if tp:
            session["transcript"] = tp
            if "offset" not in session:   # first task here: earlier usage is not ours
                try:
                    session["offset"] = os.path.getsize(tp)
                except OSError:
                    session["offset"] = 0
        if pending is not None:          # absorbs the transcript tail, then records it
            if retry and escalated_from in TIERS and tier.index > TIERS.index(escalated_from):
                pending["escalated_to"] = tier.name
            finalize(root, sid, session, "failure" if retry else "success")
        start(session, {
            "ts": int(time.time()), "class": kind, "area": area, "tier": tier.name,
            "model": tier.model, "effort": tier.effort, "risk": d.risk.score,
            "confidence": d.confidence, "source": "hook", "escalated_from": escalated_from,
            "main_model": model_alias(main)})
        session["facts"] = fact
        save_session(root, sid, session)
    except Exception as e:  # noqa: BLE001
        log(root, f"prompt bookkeeping failed: {e.__class__.__name__}: {e}")
    return Msg(text, ui_line(d, tier, kind, fact, retry))

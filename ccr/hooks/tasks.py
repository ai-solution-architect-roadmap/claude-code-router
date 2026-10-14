"""Per-session task lifecycle for outcome learning (no prompt text is ever stored).

  prompt hook   finalises the previous pending task (failure when the new prompt is a retry
                phrase such as "still failing", success otherwise: the user moved on), then
                stores the new routing decision as `pending` in
                .ccr/local/sessions/<session>.json
  stop hook     adds token usage (main transcript delta + subagent transcripts), subagent
                types, tool errors and outcome hints to the pending task
  session-start finalises pending tasks of sessions idle for a while (outcome unknown)
Finalised tasks are appended to .ccr/history.jsonl, which routing reads.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ccr.hooks.common import SESSION_TTL_S, read_json, save_session, session_path
import contextlib

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
FAIL_HINT = re.compile(
    r"\b(?:I\s+(?:was|am)\s+(?:unable|not\s+able)|I\s+(?:could|can)\s*n[o']?t|"
    r"(?:tests?|build)\s+(?:are\s+|is\s+)?still\s+failing|did\s+not\s+succeed)\b", re.I)
STALE_PENDING_S = 2 * 3600


MAX_SEEN_IDS = 400


def _task(session: dict) -> dict:
    if not isinstance(session.get("task"), dict):
        session["task"] = {}
    return session["task"]


def absorb(session: dict, main_path: str | None = None) -> None:
    """Add not-yet-counted usage from the main transcript and every subagent transcript
    seen so far. Reads are incremental (byte offsets + message ids), so calling this again
    is cheap and never double counts. Claude Code may append the final assistant message
    after Stop has run, which is why the prompt hook and the sweep call this too."""
    from ccr.transcript import read_usage
    task = _task(session)
    seen_list = list(session.get("seen_ids") or [])
    seen = set(seen_list)
    if isinstance(main_path, str) and main_path:
        session["transcript"] = main_path
    main_path = session.get("transcript")
    files = [(main_path, None)] if isinstance(main_path, str) else []
    agents = session.get("agent_files") if isinstance(session.get("agent_files"), dict) else {}
    files += [(p, info) for p, info in agents.items() if isinstance(info, dict)]
    for path, info in files:
        off = int((info if info is not None else session).get("offset") or 0)
        before = set(seen)
        u = read_usage(path, off, seen)   # complete lines only: a half-written line waits
        seen_list += [m for m in seen if m not in before]
        task["tokens"] = int(task.get("tokens") or 0) + u.tokens
        task["tool_errors"] = int(task.get("tool_errors") or 0) + u.tool_errors
        if info is None:
            session["offset"] = u.offset
            if u.model:
                mm = session.get("main_model") if isinstance(session.get("main_model"),
                                                             dict) else {}
                session["main_model"] = {**mm, "id": u.model, "ts": time.time(),
                                         "source": "transcript"}
        else:
            info["offset"] = u.offset
            if u.model:
                session.setdefault("agent_models", {})[info.get("agent", "?")] = u.model
    session["seen_ids"] = seen_list[-MAX_SEEN_IDS:]
    if task.get("tool_errors"):
        task["signals"] = sorted(set(task.get("signals") or []) | {"tool_errors"})


def is_retry(prompt: str) -> bool:
    return bool(RETRY.search(prompt[:400]))


def finalize(root: Path, sid: str | None, session: dict, outcome: str | None,
             now: float | None = None) -> dict | None:
    """Append the session's pending task to history and clear it. Returns the record."""
    pend = session.get("pending")
    if not isinstance(pend, dict):
        return None
    with contextlib.suppress(Exception):   # usage is a nice-to-have on the record
        absorb(session)
    from ccr import history as H
    rec = dict(pend)
    rec["ts"] = int(rec.get("ts") or (now or time.time()))
    rec["session"] = sid
    task = session.get("task") or {}
    for k in ("tokens", "tool_errors"):
        if task.get(k):
            rec[k] = int(task[k])
    if task.get("agents"):
        rec["agents"] = task["agents"]
    if task.get("turns"):
        rec["turns"] = int(task["turns"])
    sig = sorted(set(task.get("signals") or []))
    if sig:
        rec["signals"] = sig
    if outcome:
        rec["outcome"] = outcome
    rec.pop("feedback", None)      # feedback lives in its own records (history.resolve)
    H.append(root, H.make_record(None, **rec))
    session["pending"] = None
    session["task"] = {}
    session["agent_files"] = {}
    return rec


def start(session: dict, record: dict) -> None:
    """Make `record` the session's pending task. It gets its history id now, so
    `ccr feedback` can reference it before the task is finalised."""
    if not record.get("id"):
        from ccr.history import new_id
        record = {**record, "id": new_id()}
    session["pending"] = record
    session["task"] = {}
    session["agent_files"] = {}


def sweep(root: Path, current_sid: str | None, now: float | None = None) -> int:
    """Finalise (outcome unknown) pending tasks of idle sessions; delete old session files."""
    now = now or time.time()
    d = root / ".ccr" / "local" / "sessions"
    n = 0
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return 0
    for p in files:
        if current_sid and p == session_path(root, current_sid):
            continue
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < STALE_PENDING_S:
            continue
        data = read_json(p)
        if isinstance(data.get("pending"), dict):
            finalize(root, p.stem, data, None, now)
            n += 1
            if age < SESSION_TTL_S:
                save_session(root, p.stem, data)
        if age >= SESSION_TTL_S:
            with contextlib.suppress(OSError):
                p.unlink()
    return n

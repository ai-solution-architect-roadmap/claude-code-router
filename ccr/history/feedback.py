"""`ccr feedback good|bad`: correct the outcome of the last routed task.

The target is the newest task by timestamp among
  * finalised task records in .ccr/history.jsonl (hook tasks and `ccr run` attempts), and
  * tasks still pending in a Claude Code session (.ccr/local/sessions/*.json): the prompt
    hook gives each one its history id up front, so feedback can point at it before the
    task is written.
Feedback is appended as its own record (`type: feedback`, `ref: <id>`); history is never
rewritten and the correction is applied whenever history is read (history.resolve).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ccr import history as H


@dataclass
class Target:
    id: str
    record: dict
    where: str                 # "history" | "pending"
    session_file: Path | None = None


def _pending(root: Path) -> list[Target]:
    from ccr.hooks.common import read_json, write_json_atomic
    out = []
    d = root / ".ccr" / "local" / "sessions"
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return out
    for p in files:
        data = read_json(p)
        pend = data.get("pending")
        if not isinstance(pend, dict):
            continue
        if not isinstance(pend.get("id"), str):      # recorded by an older ccr: tag it now
            pend["id"] = H.new_id()
            try:
                write_json_atomic(p, data)
            except OSError:
                continue
        out.append(Target(pend["id"], pend, "pending", p))
    return out


def candidates(root: str | Path) -> list[Target]:
    """Every addressable task, oldest first."""
    root = Path(root)
    out = [Target(r["id"], r, "history") for r in H.read(root) if isinstance(r.get("id"), str)]
    out += _pending(root)
    out.sort(key=lambda t: float(t.record.get("ts") or 0))
    return out


def find(root: str | Path, ref: str | None = None) -> Target | None:
    cands = candidates(root)
    if ref:
        hits = [t for t in cands if t.id.startswith(ref)]
        return hits[-1] if len(hits) >= 1 and len({t.id for t in hits}) == 1 else None
    return cands[-1] if cands else None


def mark(root: str | Path, verdict: str, ref: str | None = None,
         now: float | None = None) -> tuple[Target | None, dict | None]:
    t = find(root, ref)
    if t is None:
        return None, None
    rec = H.feedback_record(t.id, verdict, now)
    H.append(root, rec)
    return t, rec


def describe(t: Target) -> str:
    r = t.record
    ts = r.get("ts")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if isinstance(
        ts, (int, float)) else "?"
    src = {"run": "ccr run", "hook": "claude session"}.get(r.get("source"), r.get("source"))
    bits = [f"{r.get('tier', '?')} {r.get('class', '?')}",
            f"area {r.get('area', '*')}", when, str(src or "?")]
    if t.where == "pending":
        bits.append("in progress")
    elif r.get("outcome"):
        bits.append(f"outcome {r['outcome']}" + (f" (feedback {r['feedback']})"
                                                 if r.get("feedback") else ""))
    return f"[{t.id[:8]}] " + ", ".join(bits)

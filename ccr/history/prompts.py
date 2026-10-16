"""Opt-in prompt + decision log: `.ccr/local/prompts.jsonl` (stdlib only; safe for hooks).

Off by default (`hooks.store_prompts`). It holds your prompt text, so it lives under
`.ccr/local/` (gitignored, never shared through history.jsonl) and is keyed by the history
task id. `ccr dashboard` joins it back to show why each task got its tier.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from ccr.history import why
from ccr.history.redact import redact as redact_text

MAX_PROMPT_CHARS = 2000
MAX_BYTES = 4 * 1024 * 1024


def path(root) -> Path:
    return Path(root) / ".ccr" / "local" / "prompts.jsonl"


def snapshot(task_id: str, prompt: str, d, tier, fact: str, notes: list[str] | None = None,
             retry: bool = False, escalated_from: str | None = None, redact: bool = True) -> dict:
    """The prompt and the decision process behind `tier` (a ccr.route.Decision `d`)."""
    tc, r = d.task_class, d.risk
    text, kinds = redact_text(prompt[:MAX_PROMPT_CHARS]) if redact else (prompt[:MAX_PROMPT_CHARS], [])
    return {
        "id": task_id, "ts": int(time.time()), "prompt": text, "redacted": kinds, "tier": tier.name,
        "model": tier.model, "effort": tier.effort, "fact": fact, "retry": retry,
        "escalated_from": escalated_from, "base": d.base, "confidence": d.confidence,
        "class": {"kind": tc.kind, "confidence": tc.confidence, "source": tc.source,
                  "secondary": tc.secondary, "scope": tc.scope, "vagueness": tc.vagueness,
                  "evidence": tc.matched.get(tc.kind, [])[:6]},
        "files": [{"path": c.path, "confidence": c.confidence, "why": "; ".join(c.reasons[:3])}
                  for c in d.location.candidates[:5]],
        "risk": {"score": r.score, "level": r.level,
                 "components": {n: {"value": c.value, "detail": c.detail}
                                for n, c in r.components.items()}},
        "why": why.from_decision(d, tier),
        "reasons": list(d.reasons), "learning": str(d.history), "budget": d.budget.detail,
        "notes": [*d.notes, *(notes or [])], "t4": list(d.t4_rules)}


def append(root, entry: dict) -> None:
    p = path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.stat().st_size > MAX_BYTES:       # keep the newest half
            keep = p.read_bytes()[-MAX_BYTES // 2:]
            p.write_bytes(keep[keep.find(b"\n") + 1:])
    except OSError:
        pass
    line = json.dumps(entry, separators=(",", ":"), default=str) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read(root) -> dict[str, dict]:
    """{task id: entry}; the newest entry wins, unreadable lines are skipped."""
    out: dict[str, dict] = {}
    try:
        text = path(root).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("id"), str):
            out[e["id"]] = e
    return out


def prune(root, days: int, now: float | None = None) -> int:
    """Drop entries older than `days` (entries without a timestamp are kept). Returns how many."""
    if days <= 0:
        return 0
    cutoff = (now or time.time()) - days * 86400
    p = path(root)
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    keep, dropped = [], 0
    for line in lines:
        try:
            ts = json.loads(line).get("ts")
        except (ValueError, AttributeError):
            ts = None
        if isinstance(ts, (int, float)) and ts < cutoff:
            dropped += 1
        else:
            keep.append(line)
    if dropped:
        p.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    return dropped


def prune_daily(root, days: int) -> int:
    """`prune`, at most once a day (cheap enough for the SessionStart hook)."""
    p = path(root)
    marker = p.with_suffix(".pruned")
    try:
        if not p.exists() or time.time() - marker.stat().st_mtime < 86400:
            return 0
    except OSError:
        if not p.exists():
            return 0
    n = prune(root, days)
    with contextlib.suppress(OSError):
        marker.write_text("")
    return n

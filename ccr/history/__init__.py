"""Routing history in `.ccr/history.jsonl` (one JSON object per line, append-only).

Records hold only routing metadata: class, area (a directory prefix), tier, model, effort,
outcome, usage numbers. Prompt text is never stored: `make_record` / `append` drop any field
outside the whitelist.

Two record types share the file:
  task      one routed task or one `ccr run` attempt (no `type` field); has a random `id`
  feedback  `{"type": "feedback", "ref": <task id>, "outcome": ..., "feedback": good|bad}`,
            written by `ccr feedback`; it corrects the task's outcome at read time
The file is never rewritten. `read()` resolves it: corrupt lines (including git conflict
markers) are skipped, duplicate lines (same `id`, or identical content for old records
without one) count once, records are ordered by `ts`, and the newest feedback for a task
wins, wherever it appears in the file. So a team can merge the file by concatenation
(`.gitattributes`: `.ccr/history.jsonl merge=union`) and nothing double counts.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

FIELDS = ("id", "type", "ref", "feedback",
          "ts", "session", "class", "area", "tier", "model", "effort", "outcome", "tokens",
          "cost_usd", "turns", "risk", "confidence", "escalated_from", "escalated_to", "source",
          # hook-recorded signals (no prompt text): main-session model, subagents used
          # ({agent_type: count}), tool errors seen, outcome hints
          "main_model", "agents", "tool_errors", "signals",
          # `ccr run` attempts: grouping, failure reason code, usage breakdown, per-model
          # usage ({model id: {tokens, cost_usd}}), test command result
          "run_id", "attempt", "reason", "subtype", "exit_code", "usage", "models",
          "duration_ms", "test", "forced", "assist_cost_usd",
          # routing signals behind the decision (Decision.trace(): no prompt text)
          "trace")
OUTCOMES = ("success", "failure", "escalated", "abandoned", "error")
SUCCESS = {"success"}
FAILURE = {"failure", "escalated"}
FEEDBACK = {"good": "success", "bad": "failure"}
MAX_READ_BYTES = 4_000_000


def history_path(root: str | Path) -> Path:
    return Path(root) / ".ccr" / "history.jsonl"


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def make_record(decision=None, outcome: str | None = None, **extra) -> dict:
    """Whitelisted task record for a routing decision (a ccr.route.Decision or a dict)."""
    rec: dict = {"ts": int(time.time())}
    if decision is not None:
        d = decision.record() if hasattr(decision, "record") else dict(decision)
        rec.update(d)
    if outcome is not None:
        rec["outcome"] = outcome
    rec.update(extra)
    if not rec.get("id"):
        rec["id"] = new_id()
    return {k: v for k, v in rec.items() if k in FIELDS and v is not None}


def feedback_record(ref: str, verdict: str, now: float | None = None) -> dict:
    if verdict not in FEEDBACK:
        raise ValueError(f"verdict must be one of {', '.join(FEEDBACK)}")
    return {"id": new_id(), "type": "feedback", "ts": int(now or time.time()), "ref": ref,
            "outcome": FEEDBACK[verdict], "feedback": verdict}


def append(root: str | Path, record: dict) -> None:
    p = history_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in record.items() if k in FIELDS}
    line = json.dumps(clean, separators=(",", ":")) + "\n"
    # one write() per record on an O_APPEND descriptor: concurrent writers do not interleave
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_raw(root: str | Path | None = None, path: str | Path | None = None) -> list[dict]:
    """Every parseable object line, in file order (no dedup, no feedback resolution)."""
    p = Path(path) if path else history_path(root)
    try:
        size = os.path.getsize(p)
    except OSError:
        return []
    try:
        with open(p, "rb") as f:
            if size > MAX_READ_BYTES:
                f.seek(size - MAX_READ_BYTES)
                f.readline()  # drop the partial first line
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line[0] != "{":     # blank, conflict markers, garbage
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def record_key(rec: dict) -> str:
    """Identity for dedup: the `id`, else the canonical content (old id-less records)."""
    rid = rec.get("id")
    if isinstance(rid, str) and rid:
        return "id:" + rid
    return "c:" + json.dumps(rec, sort_keys=True, separators=(",", ":"))


def _ts(rec: dict) -> float:
    ts = rec.get("ts")
    return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0


def resolve(raw: list[dict]) -> list[dict]:
    """Dedup, order by ts, apply the newest feedback per task. Returns task records only
    (copies; a corrected record gets `outcome` from feedback and `feedback: good|bad`)."""
    seen: set[str] = set()
    tasks: list[dict] = []
    fbs: dict[str, tuple[float, int, dict]] = {}
    for i, rec in enumerate(raw):
        k = record_key(rec)
        if k in seen:
            continue
        seen.add(k)
        if rec.get("type") == "feedback":
            ref = rec.get("ref")
            if not isinstance(ref, str) or rec.get("outcome") not in OUTCOMES:
                continue
            cur = fbs.get(ref)
            if cur is None or (_ts(rec), i) >= cur[:2]:
                fbs[ref] = (_ts(rec), i, rec)
        elif rec.get("type") is None:
            tasks.append(rec)
    tasks.sort(key=_ts)      # stable: equal timestamps keep file order
    out = []
    for rec in tasks:
        fb = fbs.get(rec.get("id")) if isinstance(rec.get("id"), str) else None
        if fb is not None:
            rec = {**rec, "outcome": fb[2]["outcome"], "feedback": fb[2].get("feedback")}
        out.append(rec)
    return out


def read(root: str | Path, window_days: float | None = None, now: float | None = None,
         path: str | Path | None = None) -> list[dict]:
    """Resolved task records (see `resolve`), optionally only those newer than the window."""
    recs = resolve(read_raw(root, path))
    if not window_days:
        return recs
    cutoff = (now if now is not None else time.time()) - window_days * 86400
    return [r for r in recs if isinstance(r.get("ts"), (int, float)) and r["ts"] >= cutoff]


def tier_stats(records: list[dict], kind: str, area: str | None) -> tuple[dict, str]:
    """{tier: [successes, failures]} for class x area, else class-wide. Returns (stats, scope)
    where scope is "area" or "class"."""
    from ccr.history.learn import tier_counts
    return tier_counts(records, kind, area)

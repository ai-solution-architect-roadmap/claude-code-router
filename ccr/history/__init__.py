"""Routing history in `.ccr/history.jsonl` (one JSON object per line, append-only).

Records hold only routing metadata: class, area (a directory prefix), tier, model, effort,
outcome, usage numbers. Prompt text is never stored: `make_record` / `append` drop any field
outside the whitelist. Readers tolerate a missing file and malformed lines.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FIELDS = ("ts", "session", "class", "area", "tier", "model", "effort", "outcome", "tokens",
          "cost_usd", "turns", "risk", "confidence", "escalated_from", "source")
OUTCOMES = ("success", "failure", "escalated", "abandoned")
SUCCESS = {"success"}
FAILURE = {"failure", "escalated"}
MAX_READ_BYTES = 4_000_000


def history_path(root: str | Path) -> Path:
    return Path(root) / ".ccr" / "history.jsonl"


def make_record(decision=None, outcome: str | None = None, **extra) -> dict:
    """Whitelisted record for a routing decision (a ccr.route.Decision or a dict)."""
    rec: dict = {"ts": int(time.time())}
    if decision is not None:
        d = decision.record() if hasattr(decision, "record") else dict(decision)
        rec.update(d)
    if outcome is not None:
        rec["outcome"] = outcome
    rec.update(extra)
    return {k: v for k, v in rec.items() if k in FIELDS and v is not None}


def append(root: str | Path, record: dict) -> None:
    p = history_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in record.items() if k in FIELDS}
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(clean, separators=(",", ":")) + "\n")


def read(root: str | Path, window_days: float | None = None, now: float | None = None,
         path: str | Path | None = None) -> list[dict]:
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
    cutoff = None
    if window_days:
        cutoff = (now if now is not None else time.time()) - window_days * 86400
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if cutoff is not None:
            ts = rec.get("ts")
            if not isinstance(ts, (int, float)) or ts < cutoff:
                continue
        out.append(rec)
    return out


def tier_stats(records: list[dict], kind: str, area: str | None) -> tuple[dict, str]:
    """{tier: [successes, failures]} for class x area, else class-wide. Returns (stats, scope)
    where scope is "area" or "class"."""
    def collect(match_area: bool) -> dict:
        st: dict[str, list[int]] = {}
        for r in records:
            if r.get("class") != kind or not isinstance(r.get("tier"), str):
                continue
            if match_area and r.get("area") != area:
                continue
            o = r.get("outcome")
            s = st.setdefault(r["tier"], [0, 0])
            if o in SUCCESS:
                s[0] += 1
            elif o in FAILURE:
                s[1] += 1
        return st
    if area and area != "*":
        st = collect(True)
        if sum(sum(v) for v in st.values()):
            return st, "area"
    return collect(False), "class"

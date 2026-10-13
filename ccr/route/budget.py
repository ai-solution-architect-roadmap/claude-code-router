"""Budget pressure from weekly subscription usage.

Primary source: `.ccr/local/state.json` written by `ccr statusline` (Claude Code passes
`rate_limits.seven_day.used_percentage` to status line commands). Fallback hook point: a
callable `(root, cfg, now) -> (used_pct | None, detail)`; the default sums token usage from
`.ccr/history.jsonl` (last 7 days) plus the current transcript, against
`budget.weekly_token_limit` when configured.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WEEK_S = 7 * 86400


@dataclass
class Budget:
    used_pct: float | None = None
    source: str = "none"          # statusline | tokens | custom | none
    pressure: bool = False
    soft_limit_pct: float = 80.0
    detail: str = "no usage data"
    age_s: int | None = None
    five_hour_pct: float | None = None


def state_path(root: str | Path) -> Path:
    return Path(root) / ".ccr" / "local" / "state.json"


def _epoch(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e12 else float(v)   # accept ms
    if isinstance(v, str) and v.strip():
        try:
            return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return None
    return None


def _pct(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return max(0.0, min(100.0, float(v)))


def read_statusline_usage(root: str | Path, cfg, now: float) -> tuple[float | None, str,
                                                                       int | None,
                                                                       float | None]:
    """(weekly used %, detail, age seconds, five-hour %) from the statusline state file."""
    p = state_path(root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        mtime = os.path.getmtime(p)
    except FileNotFoundError:
        return None, "statusline state missing", None, None
    except (OSError, ValueError) as e:
        return None, f"statusline state unreadable ({e.__class__.__name__})", None, None
    if not isinstance(data, dict):
        return None, "statusline state malformed", None, None
    rl = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {}
    week = rl.get("seven_day") if isinstance(rl.get("seven_day"), dict) else {}
    five = rl.get("five_hour") if isinstance(rl.get("five_hour"), dict) else {}
    pct = _pct(week.get("used_percentage"))
    five_pct = _pct(five.get("used_percentage"))
    ts = _epoch(data.get("ts")) or mtime
    age = max(0, int(now - ts))
    if pct is None:
        return None, "statusline state has no seven_day usage", age, five_pct
    max_age = float(cfg["budget"]["state_max_age_hours"]) * 3600
    if age > max_age:
        return None, f"statusline state is stale ({age // 3600}h old)", age, five_pct
    resets = _epoch(week.get("resets_at"))
    if resets is not None and resets <= now:
        return 0.0, "weekly window reset since last statusline update", age, five_pct
    return pct, f"weekly usage {pct:.0f}% (statusline, {_age(age)} old)", age, five_pct


def transcript_tokens(path: str | Path) -> int:
    """Input + output + cache-creation tokens, counted once per assistant message."""
    from ccr.transcript import read_usage
    return read_usage(path, complete_only=False).tokens


def token_fallback(root: str | Path, cfg, now: float,
                   transcript_path: str | Path | None = None) -> tuple[float | None, str]:
    limit = cfg["budget"].get("weekly_token_limit")
    if not limit:
        return None, "no weekly_token_limit configured for the token fallback"
    from ccr import history
    session = None
    tokens = 0
    if transcript_path:
        session = Path(transcript_path).stem
        tokens += transcript_tokens(transcript_path)
    for r in history.read(root, window_days=7, now=now):
        if session and r.get("session") == session:
            continue
        t = r.get("tokens")
        if isinstance(t, int) and not isinstance(t, bool):
            tokens += t
    pct = min(100.0, 100.0 * tokens / float(limit))
    return pct, f"~{pct:.0f}% of weekly_token_limit ({_kfmt(tokens)}/{_kfmt(limit)} tokens, " \
                f"history + transcript)"


def budget_pressure(root: str | Path, cfg, now: float | None = None, fallback=None,
                    transcript_path: str | Path | None = None) -> Budget:
    """Read weekly usage and decide whether budget pressure applies. Never raises."""
    now = time.time() if now is None else now
    soft = float(cfg["budget"]["soft_limit_pct"])
    b = Budget(soft_limit_pct=soft)
    try:
        pct, detail, age, five = read_statusline_usage(root, cfg, now)
    except Exception as e:  # pragma: no cover - defensive
        pct, detail, age, five = None, f"statusline state error: {e}", None, None
    b.age_s, b.five_hour_pct = age, five
    if pct is not None:
        b.used_pct, b.source, b.detail = pct, "statusline", detail
    else:
        why = detail
        try:
            if fallback is not None:
                fpct, fdetail = fallback(root, cfg, now)
                src = "custom"
            else:
                fpct, fdetail = token_fallback(root, cfg, now, transcript_path)
                src = "tokens"
        except Exception as e:
            fpct, fdetail, src = None, f"fallback failed: {e}", "none"
        if fpct is not None:
            b.used_pct, b.source, b.detail = float(fpct), src, f"{fdetail} [{why}]"
        else:
            b.detail = f"{why}; {fdetail}"
    if b.used_pct is not None:
        b.pressure = b.used_pct >= soft
        b.detail += (f" >= soft limit {soft:.0f}%" if b.pressure
                     else f" < soft limit {soft:.0f}%")
    return b


def _age(s: int) -> str:
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def _kfmt(n: float) -> str:
    n = float(n)
    for unit, div in (("M", 1e6), ("k", 1e3)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}"

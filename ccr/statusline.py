"""`ccr statusline [--wrap "<command>"]`: Claude Code status line relay.

Reads the status line JSON on stdin and atomically writes .ccr/local/state.json:
  {"ts", "model": {"id", "display_name"}, "effort", "rate_limits", "session_id",
   "sessions": {session_id: {"model", "effort", "ts"}}}   (newest 16 sessions)
which the prompt hook (main model for /model advice) and budget pressure (weekly usage) read.

Prints a short line (`ccr T2 sonnet/medium · 5h 24% · 7d 41%`), or with --wrap runs the
user's own status line command with the same stdin and prints its output unchanged.
Fails open: errors print nothing (or just the wrapped output) and exit 0.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

MAX_SESSIONS = 16
WRAP_TIMEOUT_S = 5.0

log = logging.getLogger("ccr.statusline")


def _shell_argv(cmd: str) -> list[str] | None:
    """How Claude Code would run a status line command: sh -c (POSIX), Git Bash or
    PowerShell (Windows)."""
    if os.name != "nt":
        return ["/bin/sh", "-c", cmd]
    from ccr.install.command import git_bash
    bash = git_bash()
    if bash:
        return [bash, "-c", cmd]
    return ["powershell", "-NoProfile", "-Command", cmd]


def run_wrapped(cmd: str, raw: bytes) -> str:
    argv = _shell_argv(cmd)
    try:
        r = subprocess.run(argv, input=raw, capture_output=True, timeout=WRAP_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode("utf-8", errors="replace")


def _pct(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def record(data: dict, environ=None) -> tuple[object, dict | None]:
    """Write state.json; returns (root, written state) or (None, None)."""
    from ccr.hooks.common import effort_level, find_root, read_json, write_json_atomic
    root = find_root(data, environ)
    if root is None:
        return None, None
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    p = root / ".ccr" / "local" / "state.json"
    prev = read_json(p)
    now = time.time()
    state = {"ts": now,
             "model": {"id": model.get("id"), "display_name": model.get("display_name")},
             "effort": effort_level(data.get("effort")),
             "rate_limits": data.get("rate_limits") if isinstance(data.get("rate_limits"),
                                                                  dict) else
             prev.get("rate_limits") if isinstance(prev.get("rate_limits"), dict) else {},
             "session_id": data.get("session_id"),
             "version": data.get("version")}
    sessions = prev.get("sessions") if isinstance(prev.get("sessions"), dict) else {}
    sid = data.get("session_id")
    if not model.get("id") and isinstance(prev.get("model"), dict) and \
            prev.get("session_id") == sid:
        state["model"] = prev["model"]            # partial payload: keep what we knew
        state["effort"] = state["effort"] or prev.get("effort")
    if isinstance(sid, str) and sid:
        old = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
        sessions[sid] = {"model": model.get("id") or old.get("model"),
                         "effort": state["effort"] or old.get("effort"), "ts": now}
        if len(sessions) > MAX_SESSIONS:
            keep = sorted(sessions.items(), key=lambda kv: -float((kv[1] or {}).get("ts") or 0))
            sessions = dict(keep[:MAX_SESSIONS])
    state["sessions"] = sessions
    write_json_atomic(p, state)
    return root, state


def line(root, state: dict) -> str:
    from ccr.hooks.common import load_session, model_alias
    parts = ["ccr"]
    sid = state.get("session_id")
    pend = load_session(root, sid).get("pending") if sid else None
    if isinstance(pend, dict) and pend.get("tier"):
        parts.append(f"{pend['tier']} {pend.get('model')}/{pend.get('effort')}")
    else:
        alias = model_alias(state["model"].get("id"))
        if alias:
            parts.append(alias + (f"/{state['effort']}" if state.get("effort") else ""))
    rl = state.get("rate_limits") or {}
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        w = rl.get(key) if isinstance(rl.get(key), dict) else {}
        v = _pct(w.get("used_percentage"))
        if v is not None:
            parts.append(f"{label} {v:.0f}%")
    return " · ".join(parts)


def main(args: list[str], *, stdin=None, stdout=None, environ=None) -> int:
    out = stdout if stdout is not None else sys.stdout
    wrap = None
    if "--wrap" in args:
        i = args.index("--wrap")
        wrap = args[i + 1] if i + 1 < len(args) else None
    text = ""
    try:
        src = stdin if stdin is not None else sys.stdin.buffer
        raw = src.read(4 * 1024 * 1024)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
    except Exception:
        log.debug("statusline: stdin unreadable", exc_info=True)
        raw = b""
    try:
        if wrap:
            text = run_wrapped(wrap, raw)
    except Exception:
        log.debug("statusline: wrapped command failed", exc_info=True)
        text = ""
    try:
        from ccr.hooks.common import parse_stdin
        data = parse_stdin(raw)
        if data is not None:
            root, state = record(data, environ)
            if not wrap and root is not None:
                text = line(root, state)
    except Exception:
        log.debug("statusline: record/line failed", exc_info=True)
    if text:
        try:
            out.write(text if text.endswith("\n") else text + "\n")
            out.flush()
        except Exception:
            log.debug("statusline: write failed", exc_info=True)
    return 0

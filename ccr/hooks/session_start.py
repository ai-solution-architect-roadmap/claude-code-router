"""SessionStart: one status line of context, plus cheap housekeeping.

  * records the session's model (SessionStart input carries `model`) for /model advice;
  * finalises pending tasks of idle sessions;
  * graph: never parsed here. A stale graph with few changed files is refreshed by a
    detached background process (`python -m ccr.hooks.bg`, offline: no grammar downloads,
    lock-protected); a large change set only gets a hint to run `ccr graph`.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from ccr.hooks.common import Msg, load_session, log, save_session
from ccr.hooks.tasks import sweep
import contextlib

CHEAP_CHANGED_FILES = 150


def spawn_refresh(root: Path) -> bool:
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": str(root),
                "env": {**os.environ, "CCR_OFFLINE": "1"}}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-m", "ccr.hooks.bg", str(root)], **kw)
        return True
    except OSError:
        return False


def graph_status(root: Path, cfg) -> str:
    from ccr.graph.query import freshness
    f = freshness(root)
    st = f["status"]
    if st == "missing":
        return "no code graph yet (run `ccr graph`); routing uses file names only"
    if st == "error":
        return "code graph unreadable, routing paused (run `ccr graph --rebuild`)"
    n = f.get("files", 0)
    if st == "fresh":
        return f"graph fresh ({n} files)"
    changed = 0
    for r in f.get("reasons", []):
        if "file(s) modified" in r:
            with contextlib.suppress(ValueError):
                changed = int(r.split()[0])
    from ccr.hooks.bg import lock_held
    if lock_held(root):
        return f"graph refresh already running ({n} files)"
    if cfg["hooks"]["background_graph"] and changed <= CHEAP_CHANGED_FILES and \
            spawn_refresh(root):
        return f"graph stale ({'; '.join(f['reasons'])}), refreshing in the background"
    return f"graph stale ({'; '.join(f['reasons'])}); run `ccr graph`"


def run(data: dict, root, cfg) -> str:
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    try:
        if sid:
            s = load_session(root, sid)
            if isinstance(data.get("model"), str) and data["model"]:
                s["main_model"] = {"id": data["model"], "ts": time.time(),
                                   "source": "session-start"}
            save_session(root, sid, s)
        sweep(root, sid)
        days = int(cfg["hooks"]["prompt_retention_days"])
        if days:
            from ccr.history import prompts
            prompts.prune_daily(root, days)
    except Exception as e:  # noqa: BLE001 - housekeeping must not cost the status line
        log(root, f"session-start housekeeping failed: {e.__class__.__name__}: {e}")
    if cfg.errors:
        return ("ccr: config error in .ccr/config.yaml, prompt routing paused "
                "(run `ccr doctor`).")
    if not cfg["hooks"]["route_prompts"]:
        return ""
    t = cfg.tiers
    tiers = ", ".join(f"{k} {v.model}/{v.effort}" for k, v in t.items())
    status = graph_status(root, cfg)
    return Msg(f"ccr: routing active, {status}. Each prompt may carry a "
               f"`ccr:` line with the cheapest sufficient tier ({tiers}); follow its "
               f"delegation hint (ccr-* subagents, Explore for search).",
               f"ccr: routing active, {status}")

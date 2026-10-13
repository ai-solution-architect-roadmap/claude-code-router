"""Stop / SubagentStop: attach outcome signals and token usage to the session's pending task.

  Stop          main transcript, read incrementally from the last offset (usage counted once
                per message id): tokens, tool errors, the model actually used, turn count
  SubagentStop  registers the subagent's own transcript (agent_transcript_path) and its
                agent_type; its usage is read now and caught up later
Claude Code can append the final assistant message after these hooks run, so the prompt
hook and the idle-session sweep catch up before a task is finalised (tasks.absorb).
Never emits output (Stop context would make Claude keep going) and never stores text: the
last assistant message is only scanned for a failure hint ("I was unable to ...").
"""
from __future__ import annotations

from ccr.hooks.common import load_session, save_session
from ccr.hooks.tasks import FAIL_HINT, absorb


def run(data: dict, root, cfg) -> None:
    sid = data.get("session_id") if isinstance(data.get("session_id"), str) else None
    if not sid:
        return None
    event = data.get("hook_event_name")
    session = load_session(root, sid)
    task = session["task"] if isinstance(session.get("task"), dict) else {}
    session["task"] = task
    signals = set(task.get("signals") or [])
    msg = data.get("last_assistant_message")
    if isinstance(msg, str) and FAIL_HINT.search(msg[-2000:]):
        signals.add("subagent_reported_failure" if event == "SubagentStop" else
                    "self_reported_failure")
    if event == "SubagentStop":
        agent = data.get("agent_type") if isinstance(data.get("agent_type"), str) else "unknown"
        agents = task.setdefault("agents", {})
        agents[agent] = int(agents.get(agent, 0)) + 1
        path = data.get("agent_transcript_path")
        if isinstance(path, str) and path:
            files = session.get("agent_files") if isinstance(session.get("agent_files"),
                                                             dict) else {}
            files.setdefault(path, {"offset": 0, "agent": agent})
            session["agent_files"] = files
    else:
        task["turns"] = int(task.get("turns") or 0) + 1
        if data.get("stop_hook_active") is True:
            signals.add("stop_hook_loop")
    task["signals"] = sorted(signals)
    tp = data.get("transcript_path")
    absorb(session, tp if isinstance(tp, str) else None)
    save_session(root, sid, session)
    return None

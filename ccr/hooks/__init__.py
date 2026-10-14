"""Claude Code hooks: `ccr hook session-start | prompt | stop`.

Contract: every hook FAILS OPEN. Any exception, bad stdin, missing/corrupt config or graph,
or a run longer than the watchdog (`hooks.watchdog_ms`, default 1.5 s) -> nothing on stdout
or stderr and exit 0. Output is produced only after the hook finished, so it is never
partial. Hook paths import neither typer nor tree-sitter and only read the graph.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
import contextlib

HOOKS = ("session-start", "prompt", "stop")
DEFAULT_WATCHDOG_S = 1.5
EVENT = {"session-start": "SessionStart", "prompt": "UserPromptSubmit"}


def _impl(name: str):
    if name == "prompt":
        from ccr.hooks.prompt import run
    elif name == "session-start":
        from ccr.hooks.session_start import run
    elif name == "stop":
        from ccr.hooks.stop import run
    else:
        return None
    return run


def _body(name: str, raw: bytes, environ, result: dict) -> tuple[str | None, object]:
    """Returns (stdout text or None, root for logging)."""
    from ccr.hooks.common import find_root, parse_stdin
    if environ.get("CCR_INTERNAL"):
        return None, None            # inside ccr's own `claude -p` calls (ccr run, LLM assist)
    data = parse_stdin(raw)
    if data is None:
        return None, None
    root = find_root(data, environ)
    if root is None:
        return None, None            # project not initialised with ccr
    if environ.get("CCR_HOOK_DEBUG"):   # field names only, never values
        _log(root, f"{name} stdin keys: {','.join(sorted(map(str, data)))}")
    from ccr.config import load_config
    result["root"] = root
    cfg = load_config(root)
    if not environ.get("CCR_HOOK_WATCHDOG_MS"):
        result["budget"] = float(cfg["hooks"]["watchdog_ms"]) / 1000.0
    if cfg.errors and name != "session-start":
        return None, root            # corrupt config: stay out of the way (doctor reports it)
    if not cfg["hooks"]["enabled"]:
        return None, root
    run = _impl(name)
    if run is None:
        return None, root
    text = run(data, root, cfg)
    if not text:
        return None, root
    event = EVENT.get(name)
    if event is None:
        return None, root
    import json
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    ui = getattr(text, "ui", None)
    if ui and cfg["hooks"]["show_in_ui"]:
        out["systemMessage"] = str(ui)
    return json.dumps(out), root


def main(args: list[str], *, stdin=None, stdout=None, environ=None,
         hard_exit: bool = True, watchdog_s: float | None = None) -> int:
    """Entry for `ccr hook <name>`. Always returns 0 (or hard-exits 0 on watchdog)."""
    t0 = time.perf_counter()
    name = args[0] if args else ""
    out = stdout if stdout is not None else sys.stdout
    env = os.environ if environ is None else environ
    if hard_exit:   # real hook process: nothing may ever reach Claude Code's stderr
        with contextlib.suppress(Exception):
            sys.stderr = io.StringIO()
    result: dict = {}

    def work():
        try:
            src = stdin if stdin is not None else sys.stdin.buffer
            raw = src.read(4 * 1024 * 1024)
            result["out"], result["root"] = _body(name, raw, env, result)
        except BaseException as e:   # noqa: BLE001 - fail open on anything
            result["err"] = e

    budget = watchdog_s
    if budget is None:
        try:
            budget = float(env.get("CCR_HOOK_WATCHDOG_MS", "")) / 1000.0
        except ValueError:
            budget = DEFAULT_WATCHDOG_S
    budget = budget or DEFAULT_WATCHDOG_S
    th = threading.Thread(target=work, name=f"ccr-hook-{name}", daemon=True)
    th.start()
    while th.is_alive():
        if watchdog_s is None and "budget" in result:   # hooks.watchdog_ms from config
            budget = result["budget"]
        left = t0 + budget - time.perf_counter()
        if left <= 0:
            break
        th.join(min(left, 0.05))
    if th.is_alive():
        _log(result.get("root"), f"{name} watchdog {budget * 1000:.0f}ms exceeded")
        if hard_exit:
            with contextlib.suppress(Exception):
                out.flush()
            os._exit(0)
        return 0
    ms = (time.perf_counter() - t0) * 1000
    if "err" in result:
        e = result["err"]
        _log(result.get("root"), f"{name} error {e.__class__.__name__}: {e} ({ms:.0f}ms)")
        return 0
    text = result.get("out")
    _log(result.get("root"), f"{name} ok {ms:.0f}ms" + ("" if text else " (silent)"))
    if text:
        try:
            out.write(text + "\n")
            out.flush()
        except Exception:
            pass
    return 0


def _log(root, line: str) -> None:
    from ccr.hooks.common import log
    log(root, line)

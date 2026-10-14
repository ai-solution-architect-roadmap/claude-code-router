"""Hooks and status line: routing context, opt-outs, task lifecycle and outcome learning,
token accounting, fail-open fault injection, import hygiene and latency."""
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ccr import history as H
from ccr.hooks import main as hook_main
from ccr.statusline import main as sl_main
from conftest import FIX, git_commit_all

PY = sys.executable
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """shop_py with git history and a built graph (copied per test)."""
    from ccr.graph import update
    root = tmp_path_factory.mktemp("hooks") / "shop"
    shutil.copytree(FIX / "route" / "shop_py", root)
    git_commit_all(root)
    update(root, workers=1, offline=True)
    return root


@pytest.fixture
def proj(built, tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(built, root)
    return root


def call(name, data, *, root=None, watchdog_s=None, raw=None):
    out = io.StringIO()
    payload = raw if raw is not None else json.dumps(data).encode()
    rc = hook_main([name], stdin=io.BytesIO(payload), stdout=out, environ={},
                   hard_exit=False, watchdog_s=watchdog_s)
    assert rc == 0
    return out.getvalue()


def prompt(root, text, sid="s1", **extra):
    out = call("prompt", {"session_id": sid, "cwd": str(root), "prompt": text,
                          "hook_event_name": "UserPromptSubmit",
                          "transcript_path": str(root / "none.jsonl"), **extra})
    if not out:
        return None
    obj = json.loads(out)
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return obj["hookSpecificOutput"]["additionalContext"]


def ui_msg(root, text, sid="s1"):
    out = call("prompt", {"session_id": sid, "cwd": str(root), "prompt": text,
                          "transcript_path": str(root / "none.jsonl")})
    return json.loads(out) if out else None


def set_main_model(root, sid, model, effort="low"):
    p = root / ".ccr" / "local" / "state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": time.time(), "model": {"id": model},
                             "sessions": {sid: {"model": model, "effort": effort,
                                                "ts": time.time()}}}))


def session(root, sid="s1"):
    return json.loads((root / ".ccr" / "local" / "sessions" / f"{sid}.json").read_text())


def all_ccr_text(root):
    return "".join(p.read_text(errors="replace") for p in (root / ".ccr").rglob("*")
                   if p.is_file() and p.suffix in (".json", ".jsonl", ".log"))


# --------------------------------------------------------------------------- prompt routing
def test_prompt_injects_routing_line(proj):
    ctx = prompt(proj, "fix the refund rounding bug in app/payments/ledger.py")
    assert ctx.startswith("ccr: T")
    assert "app/payments/ledger.py" in ctx and "critical path: payments" in ctx
    assert "Delegate implementation to ccr-" in ctx and "Explore (haiku)" in ctx
    q = prompt(proj, "where is the pagination helper defined?", sid="s9")
    assert q.startswith("ccr: T0 (haiku/low)") and "Answer directly" in q


def test_prompt_shows_decision_in_ui(proj):
    obj = ui_msg(proj, "fix the refund rounding bug in app/payments/ledger.py")
    assert set(obj) == {"hookSpecificOutput", "systemMessage"}
    assert obj["hookSpecificOutput"]["additionalContext"].startswith("ccr: T")
    msg = obj["systemMessage"]
    assert re.fullmatch(r"ccr → T\d (haiku|sonnet|opus|fable)/\w+ · ccr-[a-z]+ · .+", msg), msg
    assert "\n" not in msg and len(msg) < 120


def test_show_in_ui_off_keeps_model_context_only(proj):
    (proj / ".ccr" / "config.yaml").write_text("hooks:\n  show_in_ui: false\n")
    obj = ui_msg(proj, "fix the refund rounding bug in app/payments/ledger.py")
    assert set(obj) == {"hookSpecificOutput"}
    assert (proj / ".ccr" / "config.yaml").exists()
    out = call("session-start", {"session_id": "s1", "cwd": str(proj)})
    assert "systemMessage" not in json.loads(out)


def test_ui_message_silent_cases_stay_silent(proj):
    assert ui_msg(proj, "thanks") is None
    assert ui_msg(proj, "fix the ledger !noroute") is None


def test_session_start_ui_status(proj):
    obj = json.loads(call("session-start", {"session_id": "s1", "cwd": str(proj)}))
    assert obj["systemMessage"].startswith("ccr: routing active") and "\n" not in obj["systemMessage"]


@pytest.mark.parametrize("text", ["!noroute fix the ledger", "fix the ledger !noroute",
                                  "/compact", "  /model opus", "", "   "])
def test_prompt_opt_outs_are_silent(proj, text):
    assert prompt(proj, text) is None


def test_config_flags_disable_routing(proj):
    (proj / ".ccr" / "config.yaml").write_text("hooks:\n  route_prompts: false\n")
    assert prompt(proj, "fix the refund rounding bug in the ledger") is None
    (proj / ".ccr" / "config.yaml").write_text("hooks:\n  enabled: false\n")
    assert prompt(proj, "fix the refund rounding bug in the ledger") is None
    assert call("session-start", {"session_id": "s1", "cwd": str(proj)}) == ""


def test_chatter_is_silent_and_keeps_the_task(proj):
    assert prompt(proj, "add a test for the pagination helper")
    pend = session(proj)["pending"]
    for t in ("thanks", "yes go ahead", "ok"):
        assert prompt(proj, t) is None
    assert session(proj)["pending"] == pend
    assert H.read(proj) == []


def test_not_initialised_project_is_silent(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert prompt(tmp_path, "fix the bug in a.py") is None


# --------------------------------------------------------------------------- lifecycle
def test_retry_marks_failure_and_escalates(proj):
    first = prompt(proj, "add a test for the pagination helper")
    tier = first.split()[1]
    again = prompt(proj, "still failing, same error")
    recs = H.read(proj)
    assert len(recs) == 1 and recs[0]["outcome"] == "failure" and recs[0]["tier"] == tier
    assert recs[0]["class"] == "tests" and recs[0]["session"] == "s1"
    nxt = f"T{int(tier[1]) + 1}"
    assert again.startswith(f"ccr: {nxt} ") and f"Previous attempt ({tier})" in again
    pend = session(proj)["pending"]
    assert pend["escalated_from"] == tier and pend["class"] == "tests"
    # moving on to a new task closes the retried one as a success
    prompt(proj, "rename the variable total to grand_total in app/reports/summary.py")
    recs = H.read(proj)
    assert [r["outcome"] for r in recs] == ["failure", "success"]
    assert recs[1]["escalated_from"] == tier


@pytest.mark.parametrize("text,retry", [
    ("still failing", True), ("that didn't work", True), ("it doesn't work on staging", True),
    ("same error as before", True), ("tests still red", True), ("not fixed yet", True),
    ("try again with a smaller change", True), ("I'm still not sure how this works", False),
    ("add retry logic to the client", False), ("make the build work on windows", False)])
def test_retry_phrases(text, retry):
    from ccr.hooks.tasks import is_retry
    assert is_retry(text) is retry


def test_no_prompt_text_is_stored(proj):
    secret = "zebra-quokka-7731"
    prompt(proj, f"fix the refund rounding bug in the ledger, ticket {secret}")
    prompt(proj, f"still failing for {secret}")
    call("stop", {"session_id": "s1", "cwd": str(proj), "hook_event_name": "Stop",
                  "last_assistant_message": f"I could not fix {secret}"})
    assert secret not in all_ccr_text(proj)
    assert H.read(proj)  # something was recorded


def test_stop_counts_tokens_once_per_message_and_subagents(proj, tmp_path):
    prompt(proj, "add a test for the pagination helper")
    main_t = tmp_path / "main.jsonl"
    usage = {"input_tokens": 10, "output_tokens": 90, "cache_creation_input_tokens": 100,
             "cache_read_input_tokens": 5000}
    line = {"type": "assistant", "message": {"id": "m1", "model": "claude-sonnet-5-5",
                                             "usage": usage, "content": []}}
    err = {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True,
                                                    "content": "boom"}]}}
    main_t.write_text("\n".join(json.dumps(x) for x in (line, line, err)) + "\n")
    stop = {"session_id": "s1", "cwd": str(proj), "hook_event_name": "Stop",
            "transcript_path": str(main_t), "stop_hook_active": False,
            "last_assistant_message": "Done, tests pass."}
    assert call("stop", stop) == ""
    task = session(proj)["task"]
    assert task["tokens"] == 200 and task["tool_errors"] == 1 and task["turns"] == 1
    assert session(proj)["main_model"]["id"] == "claude-sonnet-5-5"
    # incremental: a second Stop with no new lines adds nothing
    call("stop", stop)
    assert session(proj)["task"]["tokens"] == 200
    sub_t = tmp_path / "agent.jsonl"
    sub = {"type": "assistant", "message": {"id": "a1", "model": "claude-haiku-4-5",
                                            "usage": {"input_tokens": 5, "output_tokens": 45}}}
    sub_t.write_text(json.dumps(sub) + "\n" + json.dumps(sub))
    call("stop", {"session_id": "s1", "cwd": str(proj), "hook_event_name": "SubagentStop",
                  "agent_type": "Explore", "agent_transcript_path": str(sub_t),
                  "last_assistant_message": "I was unable to find it"})
    s = session(proj)
    assert s["task"]["tokens"] == 250 and s["task"]["agents"] == {"Explore": 1}
    assert s["agent_models"]["Explore"] == "claude-haiku-4-5"
    prompt(proj, "rename the variable total to grand_total in app/reports/summary.py")
    rec = H.read(proj)[-1]
    assert rec["tokens"] == 250 and rec["agents"] == {"Explore": 1}
    assert rec["tool_errors"] == 1 and rec["outcome"] == "success"
    assert "subagent_reported_failure" in rec["signals"] and "tool_errors" in rec["signals"]


def test_sweep_finalises_idle_sessions(proj):
    prompt(proj, "add a test for the pagination helper", sid="old")
    p = proj / ".ccr" / "local" / "sessions" / "old.json"
    t = time.time() - 3 * 3600
    os.utime(p, (t, t))
    call("session-start", {"session_id": "new", "cwd": str(proj)})
    recs = H.read(proj)
    assert len(recs) == 1 and recs[0]["session"] == "old" and "outcome" not in recs[0]
    assert session(proj, "old")["pending"] is None


# --------------------------------------------------------------------------- model advice
def test_model_advice_for_iterative_work_above_main_model(proj):
    set_main_model(proj, "s1", "claude-haiku-4-5")
    ctx = prompt(proj, "the checkout flow intermittently double charges customers, "
                       "figure out why")
    assert "`/model opus`" in ctx and "`/model haiku`" in ctx and "debugging" in ctx
    plain = prompt(proj, "add a test for the pagination helper", sid="s2")
    assert "/model" not in plain
    (proj / ".ccr" / "config.yaml").write_text("hooks:\n  model_advice: false\n")
    ctx = prompt(proj, "the checkout flow intermittently double charges customers, "
                       "figure out why", sid="s1")
    assert "/model" not in ctx


def test_main_session_already_at_tier_handles_directly(proj):
    set_main_model(proj, "s1", "claude-sonnet-5-5", effort="high")
    ctx = prompt(proj, "add a test for the pagination helper")
    assert "handle it directly" in ctx and "Delegate" not in ctx


def test_session_start_records_model_and_reports_graph(proj, monkeypatch):
    out = call("session-start", {"session_id": "s1", "cwd": str(proj),
                                 "model": "claude-haiku-4-5", "source": "startup"})
    ctx = json.loads(out)["hookSpecificOutput"]
    assert ctx["hookEventName"] == "SessionStart"
    assert "graph fresh" in ctx["additionalContext"]
    assert session(proj)["main_model"]["id"] == "claude-haiku-4-5"
    # stale graph with few changes -> background refresh (never parsed in the hook)
    from ccr.hooks import session_start as SS
    spawned = []
    monkeypatch.setattr(SS, "spawn_refresh", lambda root: spawned.append(root) or True)
    f = proj / "app" / "api" / "pagination.py"
    f.write_text(f.read_text() + "\n# touched\n")
    out = call("session-start", {"session_id": "s1", "cwd": str(proj)})
    assert "refreshing in the background" in out and spawned == [proj]


def test_background_refresh_updates_graph(proj):
    from ccr.graph.query import freshness
    from ccr.hooks import bg
    f = proj / "app" / "api" / "pagination.py"
    f.write_text(f.read_text() + "\n\ndef extra_helper():\n    return 2\n")
    assert freshness(proj)["status"] == "stale"
    assert bg.main([str(proj)]) == 0
    assert freshness(proj)["status"] == "fresh" and not bg.lock_held(proj)
    bg.acquire(proj)
    assert bg.lock_held(proj) and bg.acquire(proj) is False


# --------------------------------------------------------------------------- status line
STATUS = {"session_id": "s1", "model": {"id": "claude-sonnet-5-5", "display_name": "Sonnet"},
          "effort": {"level": "medium"}, "version": "2.1.289",
          "rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": 9999999999},
                          "seven_day": {"used_percentage": 41.2, "resets_at": 9999999999}}}


def sl(args, data=None, raw=None):
    out = io.StringIO()
    payload = raw if raw is not None else json.dumps(data).encode()
    assert sl_main(args, stdin=io.BytesIO(payload), stdout=out, environ={}) == 0
    return out.getvalue()


def test_statusline_writes_state_and_prints_line(proj):
    out = sl([], {**STATUS, "workspace": {"project_dir": str(proj)}})
    assert out == "ccr · sonnet/medium · 5h 24% · 7d 41%\n"
    st = json.loads((proj / ".ccr" / "local" / "state.json").read_text())
    assert st["model"]["id"] == "claude-sonnet-5-5" and st["effort"] == "medium"
    assert st["rate_limits"]["seven_day"]["used_percentage"] == 41.2
    assert st["sessions"]["s1"]["model"] == "claude-sonnet-5-5"
    assert not list((proj / ".ccr" / "local").glob(".*.tmp"))
    # budget pressure reads the relay
    from ccr.config import load_config
    from ccr.route.budget import budget_pressure
    b = budget_pressure(proj, load_config(proj))
    assert b.source == "statusline" and b.used_pct == pytest.approx(41.2)
    # after routing, the line shows the tier; a partial payload keeps the known model
    prompt(proj, "add a test for the pagination helper")
    out = sl([], {"session_id": "s1", "cwd": str(proj)})
    assert out.startswith("ccr · T1 sonnet/low")
    assert json.loads((proj / ".ccr" / "local" / "state.json").read_text()
                      )["sessions"]["s1"]["model"] == "claude-sonnet-5-5"


def test_statusline_wrap_passes_stdin_and_prints_theirs(proj):
    data = {**STATUS, "cwd": str(proj)}
    out = sl(["--wrap", "python3 -c \"import sys,json; d=json.load(sys.stdin); "
                        "print('mine', d['model']['display_name'])\""], data)
    assert out == "mine Sonnet\n"
    assert (proj / ".ccr" / "local" / "state.json").exists()
    assert sl(["--wrap", "exit 3"], data) == ""


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1,2]", b"\xff\xfe\x00garbage",
                                 b'{"model": 5, "rate_limits": "x", "cwd": 7}'])
def test_statusline_bad_input_is_silent(raw):
    assert sl([], raw=raw) == ""


# --------------------------------------------------------------------------- fault injection
@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"\x00\xff" * 100,
                                 b'{"prompt": 5, "cwd": ["x"]}',
                                 b'{"session_id": {"a": 1}, "prompt": "fix it"}'])
@pytest.mark.parametrize("name", ["prompt", "session-start", "stop", "bogus"])
def test_bad_stdin_is_silent(name, raw):
    assert call(name, None, raw=raw) == ""


def test_corrupt_graph_is_silent(proj):
    (proj / ".ccr" / "graph.db").write_bytes(b"this is not a sqlite database" * 100)
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(str(proj / ".ccr" / "graph.db") + suffix)
        except OSError:
            pass
    assert prompt(proj, "fix the refund rounding bug in the ledger") is None
    out = call("session-start", {"session_id": "s1", "cwd": str(proj)})
    assert "unreadable" in out     # the one place a problem is mentioned (to Claude)


@pytest.mark.parametrize("cfg", ["tiers: [unclosed", "tiers:\n  T9: {model: gpt}\n",
                                 "- just\n- a list\n", "hooks:\n  enabled: maybe\n"])
def test_corrupt_config_is_silent(proj, cfg):
    (proj / ".ccr" / "config.yaml").write_text(cfg)
    assert prompt(proj, "fix the refund rounding bug in the ledger") is None
    assert call("stop", {"session_id": "s1", "cwd": str(proj),
                         "hook_event_name": "Stop"}) == ""


def test_exceptions_are_silent(proj, monkeypatch):
    import ccr.route

    def boom(*a, **k):
        raise RuntimeError("injected")
    monkeypatch.setattr(ccr.route, "decide", boom)
    assert prompt(proj, "fix the refund rounding bug in the ledger") is None
    log = (proj / ".ccr" / "local" / "hooks.log").read_text()
    assert "prompt error RuntimeError: injected" in log


def test_unwritable_state_still_routes_or_stays_silent(proj, monkeypatch):
    from ccr.hooks import common
    monkeypatch.setattr(common, "write_json_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    ctx = prompt(proj, "add a test for the pagination helper")
    assert ctx is None or ctx.startswith("ccr: T")


def test_watchdog_in_process(proj, monkeypatch):
    import ccr.route

    def slow(*a, **k):
        time.sleep(2)
    monkeypatch.setattr(ccr.route, "decide", slow)
    t = time.perf_counter()
    assert call("prompt", {"session_id": "s1", "cwd": str(proj), "prompt": "fix the ledger"},
                watchdog_s=0.2) == ""
    assert time.perf_counter() - t < 1.0
    assert "watchdog" in (proj / ".ccr" / "local" / "hooks.log").read_text()


def _proc(args, stdin_bytes, cwd, env_extra=None, close_stdin=True, timeout=20):
    env = {**os.environ, "PYTHONPATH": str(REPO), **(env_extra or {})}
    env.pop("CLAUDE_PROJECT_DIR", None)
    p = subprocess.Popen([PY, "-m", "ccr", *args], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env)
    if close_stdin:
        out, err = p.communicate(stdin_bytes, timeout=timeout)
    else:   # keep stdin open: the hook blocks reading it until the watchdog fires
        p.stdin.write(stdin_bytes)
        p.stdin.flush()
        p.wait(timeout=timeout)
        out, err = p.stdout.read(), p.stderr.read()
        p.stdin.close()
    return p.returncode, out, err


@pytest.mark.parametrize("case", ["bad-json", "corrupt-graph", "corrupt-config", "missing-transcript",
                                  "stdin-never-closes"])
def test_fault_injection_real_process(proj, case):
    """The real `python -m ccr hook ...` process: exit 0, empty stdout AND stderr."""
    data = {"session_id": "s1", "cwd": str(proj), "prompt": "fix the refund bug in the ledger"}
    name, payload, env, close = "prompt", json.dumps(data).encode(), {}, True
    if case == "bad-json":
        payload = b"{\"prompt\": "
    elif case == "corrupt-graph":
        (proj / ".ccr" / "graph.db").write_bytes(b"garbage" * 1000)
    elif case == "corrupt-config":
        (proj / ".ccr" / "config.yaml").write_text("tiers: {T0: [")
    elif case == "missing-transcript":
        name = "stop"
        payload = json.dumps({**data, "hook_event_name": "Stop",
                              "transcript_path": str(proj / "nope.jsonl")}).encode()
    elif case == "stdin-never-closes":
        env, close = {"CCR_HOOK_WATCHDOG_MS": "300"}, False
    t = time.perf_counter()
    rc, out, err = _proc(["hook", name], payload, proj, env, close_stdin=close)
    assert (rc, out, err) == (0, b"", b"")
    if case == "stdin-never-closes":
        assert time.perf_counter() - t < 5


def test_hook_paths_do_not_import_typer_or_tree_sitter(proj):
    code = ("import io,json,sys; from ccr.hooks import main; "
            f"d=json.dumps({{'session_id':'s1','cwd':{str(proj)!r},"
            "'prompt':'fix the refund rounding bug in the ledger'}).encode(); "
            "o=io.StringIO(); main(['prompt'], stdin=io.BytesIO(d), stdout=o, environ={}, "
            "hard_exit=False); "
            "main(['session-start'], stdin=io.BytesIO(d), stdout=io.StringIO(), environ={}, "
            "hard_exit=False); "
            "from ccr.statusline import main as s; s([], stdin=io.BytesIO(d), "
            "stdout=io.StringIO(), environ={}); "
            "bad=[m for m in sys.modules if m.split('.')[0] in "
            "('typer','click','tree_sitter','tree_sitter_language_pack','ccr.graph.build')"
            " or m in ('ccr.graph.build','ccr.graph.parse','ccr.cli')]; "
            "print(json.dumps({'ctx': bool(o.getvalue()), 'bad': bad}))")
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, cwd=REPO, timeout=60)
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res == {"ctx": True, "bad": []}


# --------------------------------------------------------------------------- latency
def test_prompt_hook_latency(proj, capsys):
    """Warm in-process prompt hook < 500 ms; also reports the real process latency."""
    texts = ["fix the refund rounding bug in app/payments/ledger.py",
             "add a test for the pagination helper",
             "the checkout flow intermittently double charges customers, figure out why",
             "where is the pagination helper defined?"]
    prompt(proj, texts[0], sid="warm")          # warm caches (imports, page cache)
    warm = []
    for i in range(3):
        for t in texts:
            t0 = time.perf_counter()
            assert prompt(proj, t, sid=f"b{i}")
            warm.append((time.perf_counter() - t0) * 1000)
    proc = []
    payload = json.dumps({"session_id": "p", "cwd": str(proj), "prompt": texts[0]}).encode()
    for _ in range(5):
        t0 = time.perf_counter()
        rc, out, err = _proc(["hook", "prompt"], payload, proj)
        proc.append((time.perf_counter() - t0) * 1000)
        assert rc == 0 and out.startswith(b'{"hookSpecificOutput"') and err == b""
    with capsys.disabled():
        print(f"\n  prompt hook warm in-process: median {statistics.median(warm):.0f} ms, "
              f"max {max(warm):.0f} ms (n={len(warm)}); real process `python -m ccr hook "
              f"prompt`: median {statistics.median(proc):.0f} ms, max {max(proc):.0f} ms")
    assert statistics.median(warm) < 500 and max(warm) < 1000
    assert statistics.median(proc) < 1500


def test_claude_config_files_are_not_routing_candidates(proj):
    from ccr.install import apply, plan_init
    from ccr.install.command import HookCommand
    from ccr.route import decide
    apply(plan_init(proj, hook_command=HookCommand(["ccr"]), environ={"HOME": str(proj)}))
    git_commit_all(proj)
    d = decide("use the explore agent to implement the search tests", proj)
    assert not [c.path for c in d.location.candidates if c.path.startswith(".claude/")]
    d = decide("tighten the description in .claude/agents/ccr-small.md", proj)
    assert ".claude/agents/ccr-small.md" in d.location.explicit


@pytest.mark.parametrize("text", [
    "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>",
    "<command-name>/model</command-name>", "<local-command-stdout>ok</local-command-stdout>",
    "  <bash-stdout>x</bash-stdout>"])
def test_synthetic_user_turns_are_ignored(proj, text):
    assert prompt(proj, "add a test for the pagination helper")
    pend = session(proj)["pending"]
    assert prompt(proj, text) is None
    assert session(proj)["pending"] == pend and H.read(proj) == []


def test_html_prompt_still_routes(proj):
    assert prompt(proj, "<div> in app/api/handlers.py renders twice, fix the handler")


def test_usage_written_after_stop_is_caught_up(proj, tmp_path):
    """Seen in a real `claude -p` run: the final assistant message lands in the transcript
    after the Stop hook ran. The next prompt (or the idle sweep) must still count it."""
    t = tmp_path / "main.jsonl"
    t.write_text(json.dumps({"type": "user", "message": {"content": "old turn"}}) + "\n" +
                 json.dumps({"type": "assistant", "message": {
                     "id": "old", "usage": {"input_tokens": 999}}}) + "\n")
    prompt(proj, "add a test for the pagination helper", transcript_path=str(t))
    assert session(proj)["offset"] == t.stat().st_size      # earlier usage is not this task's
    call("stop", {"session_id": "s1", "cwd": str(proj), "hook_event_name": "Stop",
                  "transcript_path": str(t)})
    assert session(proj)["task"].get("tokens", 0) == 0     # nothing flushed yet
    with open(t, "a") as f:                                 # ...now Claude Code writes it
        f.write(json.dumps({"type": "assistant", "message": {
            "id": "late", "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 3, "output_tokens": 7}}}) + "\n")
    prompt(proj, "rename the variable total to grand_total in app/reports/summary.py",
           transcript_path=str(t))
    rec = H.read(proj)[-1]
    assert rec["tokens"] == 10 and rec["outcome"] == "success" and rec["turns"] == 1
    assert session(proj)["main_model"]["id"] == "claude-haiku-4-5"

"""`ccr run`: claude -p invocation, result parsing (real 2.1.x output samples), success
criteria, one-tier escalation, budget, history records. A fake `claude` shim on PATH stands
in for the real CLI."""
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ccr import headless as R
from ccr import history as H
from ccr.cli import app
from ccr.config import default_config
from conftest import FIX, git_commit_all

RUN_FIX = FIX / "run"
PAGINATE = "Fix the off-by-one error in paginate() in app/api/pagination.py"
SECRET = "SECRET-PROMPT-TEXT-1234"
cli = CliRunner()

SHIM = r'''#!{py}
"""Fake `claude`: answers by --model/--effort from $CCR_SHIM_PLAN, logs each call."""
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read()
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
key = f"{{opt('--model')}}/{{opt('--effort')}}"
log = os.environ.get("CCR_SHIM_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": argv, "stdin": stdin, "key": key,
                             "internal": os.environ.get("CCR_INTERNAL"),
                             "effort_env": os.environ.get("CLAUDE_CODE_EFFORT_LEVEL")}}) + "\n")
if "--tools" in argv:          # ccr's LLM-assist call: give no usable classification
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": "no idea", "total_cost_usd": 0.001}}))
    sys.exit(0)
plan = json.loads(os.environ.get("CCR_SHIM_PLAN", "{{}}"))
fixture, code = plan.get(key, ["success.json", 0])
if fixture == "garbage":
    print("segfault-ish noise"); sys.exit(3)
with open(os.path.join(os.environ["CCR_SHIM_FIX"], fixture)) as f:
    print(f.read().replace("\n", " "))
sys.exit(code)
'''


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from ccr.graph import update
    root = tmp_path_factory.mktemp("run") / "shop"
    shutil.copytree(FIX / "route" / "shop_py", root)
    git_commit_all(root)
    update(root, workers=1, offline=True)
    (root / ".ccr" / "history.jsonl").write_text("")
    return root


@pytest.fixture
def proj(built, tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(built, root)
    return root


@pytest.fixture
def shim(tmp_path, monkeypatch):
    """Put a fake `claude` first on PATH; returns (set_plan, calls)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "claude"
    exe.write_text(SHIM.format(py=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "shim.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CCR_SHIM_LOG", str(log))
    monkeypatch.setenv("CCR_SHIM_FIX", str(RUN_FIX))
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")   # must not reach the child

    def set_plan(plan):
        monkeypatch.setenv("CCR_SHIM_PLAN", json.dumps(plan))

    def calls():
        if not log.exists():
            return []
        return [c for c in map(json.loads, log.read_text().splitlines())
                if "--tools" not in c["argv"]]
    return set_plan, calls


def fixture_json(name):
    return json.loads((RUN_FIX / name).read_text())


# ------------------------------------------------------------------ result parsing (real)
def test_parse_real_success_output():
    raw = (RUN_FIX / "success.json").read_text()
    data = R.parse_result(raw)
    assert data["subtype"] == "success" and data["is_error"] is False
    a = R.Attempt("T0", "haiku", "low")
    R.absorb_result(a, data)
    mu = next(iter(fixture_json("success.json")["modelUsage"].values()))
    assert a.cost_usd == pytest.approx(fixture_json("success.json")["total_cost_usd"])
    assert a.turns == 1 and a.result == "ok"
    assert a.tokens == mu["inputTokens"] + mu["outputTokens"] + mu["cacheCreationInputTokens"]
    assert set(a.usage) <= {"input", "output", "cache_creation", "cache_read"}
    assert list(a.models) == ["claude-haiku-4-5-20251001"]


@pytest.mark.parametrize("name", ["error_max_turns.json", "error_max_budget_usd.json"])
def test_parse_real_error_outputs(name):
    data = R.parse_result("some warning on stdout\n" + (RUN_FIX / name).read_text()
                          .replace("\n", " "))
    assert data["subtype"] == name[:-5] and data["is_error"] is True
    a = R.Attempt("T1", "sonnet", "low")
    R.absorb_result(a, data)
    assert a.result == "" and a.cost_usd > 0 and a.turns >= 1


def test_parse_rejects_noise():
    assert R.parse_result("") is None and R.parse_result("not json") is None
    assert R.parse_result('{"hello": 1}') is None


def test_build_argv_flags():
    t = default_config().tier("T2")
    argv = R.build_argv("/x/claude", t, max_turns=7, max_budget_usd=0.25,
                        permission_mode="acceptEdits")
    assert argv == ["/x/claude", "-p", "--model", "sonnet", "--effort", "medium",
                    "--output-format", "json", "--max-turns", "7", "--max-budget-usd",
                    "0.25", "--permission-mode", "acceptEdits"]
    assert "--max-budget-usd" not in R.build_argv("c", t, max_turns=1, max_budget_usd=None,
                                                  permission_mode=None)


# ------------------------------------------------------------------ escalation via the CLI
def test_t1_failure_escalates_to_t2_and_history_shows_both(proj, shim):
    set_plan, calls = shim
    set_plan({"sonnet/low": ["error_max_turns.json", 1], "sonnet/medium": ["success.json", 0]})
    r = cli.invoke(app, ["run", f"{PAGINATE} {SECRET}", "--path", str(proj),
                         "--max-turns", "5"])
    assert r.exit_code == 0, r.output
    assert "routed T1" in r.output and "escalating T1 -> T2" in r.output
    recs = H.read(proj)
    assert [(x["tier"], x["outcome"]) for x in recs] == [("T1", "failure"), ("T2", "success")]
    t1, t2 = recs
    assert t1["reason"] == "error_max_turns" and t1["escalated_to"] == "T2"
    assert t1["exit_code"] == 1 and t1["subtype"] == "error_max_turns"
    assert t2["escalated_from"] == "T1" and t2["reason"] == "success"
    assert t1["run_id"] == t2["run_id"] and (t1["attempt"], t2["attempt"]) == (1, 2)
    assert t1["class"] == t2["class"] == "bugfix" and t1["area"] == "app/api"
    assert t1["source"] == "run" and t1["model"] == "sonnet" and t1["effort"] == "low"
    assert t1["cost_usd"] == pytest.approx(fixture_json("error_max_turns.json")
                                           ["total_cost_usd"])
    assert t2["tokens"] > 0 and t2["usage"]["output"] > 0 and t2["models"]
    for x in recs:
        assert set(x) <= set(H.FIELDS)
    raw = H.history_path(proj).read_text()
    assert SECRET not in raw and "paginate" not in raw
    c = calls()
    assert [x["key"] for x in c] == ["sonnet/low", "sonnet/medium"]
    for x in c:
        a = x["argv"]
        assert a[0] == "-p" and a[a.index("--output-format") + 1] == "json"
        assert a[a.index("--max-turns") + 1] == "5"
        assert SECRET not in " ".join(a)            # task goes on stdin only
        assert x["internal"] == "1" and x["effort_env"] is None
    assert SECRET in c[0]["stdin"] and "[ccr]" not in c[0]["stdin"]
    assert "previous attempt at T1" in c[1]["stdin"] and "ran out of turns" in c[1]["stdin"]


def test_escalation_counts_as_failure_for_learning(proj, shim):
    set_plan, _ = shim
    set_plan({"sonnet/low": ["error_max_turns.json", 1]})
    for _ in range(3):
        cli.invoke(app, ["run", PAGINATE, "--path", str(proj)])
    r = cli.invoke(app, ["explain", PAGINATE, "--path", str(proj)])
    assert "Learning:  T1 failed 3/3 for bugfix in app/api: +1" in r.output
    assert "Tier:      T2" in r.output


def test_run_stops_after_max_escalations(proj, shim):
    set_plan, calls = shim
    set_plan({"sonnet/low": ["error_max_turns.json", 1],
              "sonnet/medium": ["error_max_budget_usd.json", 1]})
    r = cli.invoke(app, ["run", PAGINATE, "--path", str(proj)])
    assert r.exit_code == 1 and "max_escalations 1 reached" in r.output
    recs = H.read(proj)
    assert [(x["tier"], x["outcome"], x.get("escalated_to")) for x in recs] == [
        ("T1", "failure", "T2"), ("T2", "failure", None)]
    assert recs[1]["reason"] == "error_max_budget_usd"


def test_more_escalations_climb_one_tier_at_a_time(proj, shim):
    set_plan, calls = shim
    set_plan({"sonnet/low": ["error_max_turns.json", 1],
              "sonnet/medium": ["error_max_turns.json", 1], "opus/high": ["success.json", 0]})
    r = cli.invoke(app, ["run", PAGINATE, "--max-escalations", "3", "--path", str(proj)])
    assert r.exit_code == 0
    assert [x["key"] for x in calls()] == ["sonnet/low", "sonnet/medium", "opus/high"]
    # T3 failure: T4 needs a critical-path rule, so the run stops there
    set_plan({"opus/high": ["error_max_turns.json", 1]})
    r = cli.invoke(app, ["run", PAGINATE, "--tier", "T3", "--max-escalations", "3",
                         "--path", str(proj)])
    assert r.exit_code == 1 and "T4 needs a critical-path rule" in r.output
    last = H.read(proj)[-1]
    assert last["tier"] == "T3" and last["forced"] is True and "escalated_to" not in last


def test_zero_escalations_and_exit_code_mismatch(proj, shim):
    set_plan, _ = shim
    # exit 1 with a "success" body still fails (exit code is part of the criteria)
    set_plan({"sonnet/low": ["success.json", 1]})
    r = cli.invoke(app, ["run", PAGINATE, "--max-escalations", "0", "--path", str(proj)])
    assert r.exit_code == 1
    rec = H.read(proj)[-1]
    assert rec["outcome"] == "failure" and rec["reason"] == "exit_1"


def test_unparseable_output_is_an_error_not_a_tier_failure(proj, shim):
    set_plan, calls = shim
    set_plan({"sonnet/low": ["garbage", 3]})
    r = cli.invoke(app, ["run", PAGINATE, "--path", str(proj)])
    assert r.exit_code == 2 and len(calls()) == 1
    rec = H.read(proj)[-1]
    assert rec["outcome"] == "error" and rec["reason"] == "no_json_exit_3"


def test_missing_claude_is_an_error(proj, monkeypatch):
    monkeypatch.setenv("PATH", str(proj / "nowhere"))
    res = R.run_task(PAGINATE, proj, cfg=default_config(), allow_llm=False)
    assert not res.success and res.attempts[0].reason == "claude_not_found"
    assert len(res.attempts) == 1


def test_dry_run_runs_nothing(proj, shim):
    _, calls = shim
    r = cli.invoke(app, ["run", PAGINATE, "--dry-run", "--no-llm", "--path", str(proj)])
    assert r.exit_code == 0 and "would run:" in r.output and "--model sonnet" in r.output
    assert calls() == [] and H.read(proj) == []


# ------------------------------------------------------------------ test command, budget
def _fake_runner(plan, calls):
    def runner(argv, stdin_text, cwd, env, timeout):
        key = f"{argv[argv.index('--model') + 1]}/{argv[argv.index('--effort') + 1]}"
        calls.append({"key": key, "stdin": stdin_text, "argv": argv, "env": env})
        name, code = plan.get(key, ("success.json", 0))
        return R.Proc(code, (RUN_FIX / name).read_text())
    return runner


def test_failing_test_command_escalates_with_its_output(proj):
    calls, tests = [], []

    def test_runner(cmd, cwd, env, timeout):
        tests.append(cmd)
        assert "CCR_INTERNAL" not in env
        return R.Proc(1 if len(tests) == 1 else 0, "FAILED test_paginate - assert 3 == 2\n")
    res = R.run_task(PAGINATE, proj, cfg=default_config(), allow_llm=False, claude_exe="claude",
                     test_command="pytest -q", runner=_fake_runner({}, calls),
                     test_runner=test_runner)
    assert res.success and [a.tier for a in res.attempts] == ["T1", "T2"]
    assert res.attempts[0].reason == "test_failed" and res.attempts[0].test == "fail"
    assert res.attempts[1].test == "pass"
    assert "assert 3 == 2" in calls[1]["stdin"]
    recs = H.read(proj)
    assert [r["test"] for r in recs] == ["fail", "pass"]
    assert "assert 3 == 2" not in H.history_path(proj).read_text()


def test_config_test_command_and_no_test(proj):
    cfg = default_config()
    cfg.data["run"]["test_command"] = "false"
    seen = []
    res = R.run_task(PAGINATE, proj, cfg=cfg, allow_llm=False, use_tests=False,
                     claude_exe="claude",
                     runner=_fake_runner({}, seen),
                     test_runner=lambda *a: pytest.fail("tests must not run"))
    assert res.success and res.attempts[0].test is None


def test_budget_is_shared_across_attempts(proj):
    calls = []
    first = fixture_json("error_max_turns.json")["total_cost_usd"]
    res = R.run_task(PAGINATE, proj, cfg=default_config(), allow_llm=False,
                     claude_exe="claude", max_budget_usd=first + 0.5,
                     runner=_fake_runner({"sonnet/low": ("error_max_turns.json", 1)}, calls))
    assert res.success and len(calls) == 2
    b0 = float(calls[0]["argv"][calls[0]["argv"].index("--max-budget-usd") + 1])
    b1 = float(calls[1]["argv"][calls[1]["argv"].index("--max-budget-usd") + 1])
    assert b0 == pytest.approx(first + 0.5, abs=1e-3) and b1 == pytest.approx(0.5, abs=1e-3)
    calls.clear()
    res = R.run_task(PAGINATE, proj, cfg=default_config(), allow_llm=False,
                     claude_exe="claude", max_budget_usd=first / 2,
                     runner=_fake_runner({"sonnet/low": ("error_max_turns.json", 1)}, calls))
    assert not res.success and len(calls) == 1 and "budget" in res.stopped


def test_run_never_sets_effort_env(proj):
    calls = []
    R.run_task(PAGINATE, proj, cfg=default_config(), allow_llm=False, claude_exe="claude",
               environ={"PATH": os.environ["PATH"], "CLAUDE_CODE_EFFORT_LEVEL": "max"},
               runner=_fake_runner({}, calls))
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in calls[0]["env"]
    assert calls[0]["env"]["CCR_INTERNAL"] == "1"


def test_hooks_are_silent_inside_ccr_internal_calls(proj):
    import io

    from ccr.hooks import main as hook_main
    out = io.StringIO()
    data = {"session_id": "s1", "cwd": str(proj), "prompt": PAGINATE,
            "hook_event_name": "UserPromptSubmit"}
    hook_main(["prompt"], stdin=io.BytesIO(json.dumps(data).encode()), stdout=out,
              environ={"CCR_INTERNAL": "1"}, hard_exit=False)
    assert out.getvalue() == ""
    assert not (proj / ".ccr" / "local" / "sessions").exists()
    hook_main(["prompt"], stdin=io.BytesIO(json.dumps(data).encode()), stdout=out,
              environ={}, hard_exit=False)
    assert out.getvalue().startswith('{"hookSpecificOutput"')


def test_json_summary(proj, shim):
    set_plan, _ = shim
    set_plan({})
    r = cli.invoke(app, ["run", PAGINATE, "--json", "--path", str(proj)])
    assert r.exit_code == 0
    body = r.output[r.output.index("{\n"):r.output.rindex("}") + 1]
    s = json.loads(body)
    assert s["success"] is True and s["attempts"][0]["tier"] == "T1"
    assert s["cost_usd"] == pytest.approx(fixture_json("success.json")["total_cost_usd"])


def test_empty_task_and_bad_tier(proj):
    assert cli.invoke(app, ["run", "  ", "--path", str(proj)]).exit_code == 2
    assert cli.invoke(app, ["run", "x", "--tier", "T9", "--path", str(proj)]).exit_code == 2


def test_stdin_task(proj, shim):
    set_plan, calls = shim
    set_plan({})
    r = cli.invoke(app, ["run", "-", "--no-llm", "--path", str(proj)], input=PAGINATE)
    assert r.exit_code == 0 and calls()[0]["stdin"] == PAGINATE
    assert Path(proj / ".ccr" / "history.jsonl").read_text().count("\n") == 1

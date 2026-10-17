"""Manual-escalation and rework signals, counterfactual sampling, and `ccr tune`."""
import json
import time

from ccr import history as H
from ccr.config import load_config
from ccr.history import learn, make_record, shadow, tune
from test_hooks import built, proj, prompt  # noqa: F401  (fixtures)


def _rec(tier, outcome, cls="bugfix", area="a", **kw):
    return make_record({"tier": tier, "class": cls, "area": area}, outcome=outcome, **kw)


def test_rework_phrase_on_same_code_flags_previous_task(proj):  # noqa: F811
    prompt(proj, "fix the off-by-one in paginate() in app/api/pagination.py")
    prompt(proj, "not quite, I meant the page_links helper in app/api/pagination.py")
    (r,) = H.read(proj)
    assert "possible_rework" in (r.get("signals") or [])


def test_plain_iteration_is_not_rework(proj):  # noqa: F811
    prompt(proj, "fix the off-by-one in paginate() in app/api/pagination.py")
    prompt(proj, "now add a test for paginate in app/api/pagination.py")
    (r,) = H.read(proj)
    assert "possible_rework" not in (r.get("signals") or [])


def test_rework_only_counts_when_it_repeats():
    cfg = load_config("/nonexistent")
    one = [_rec("T1", "success", signals=["possible_rework"])]
    assert not any(r.get("struggled") for r in learn.mark_struggle(one, cfg))
    two = one + [_rec("T1", "success", signals=["possible_rework"])]
    assert all(r.get("struggled") for r in learn.mark_struggle(two, cfg))


def test_manual_model_up_counts_as_struggle():
    cfg = load_config("/nonexistent")
    r = _rec("T1", "success", signals=["manual_model_up"])
    assert learn.struggled(r, cfg["history"])


def test_shadow_picks_only_deep_successes_with_prompts():
    deep, cheap, nop = _rec("T3", "success"), _rec("T1", "success"), _rec("T3", "success")
    logged = {deep["id"]: {"prompt": "p"}, cheap["id"]: {"prompt": "p"}}
    got = shadow.pick([deep, cheap, nop], logged, set(), "T3", 5)
    assert [r["id"] for r in got] == [deep["id"]]
    assert shadow.pick([deep], logged, {deep["id"]}, "T3", 5) == []
    assert shadow.shadow_tier("T3", None) == "T2H"


def test_shadow_summary_and_run_with_fake_runner(tmp_path, monkeypatch):
    from ccr.headless import Proc
    monkeypatch.setattr("ccr.headless.find_claude", lambda env: "claude")

    def runner(argv, stdin, cwd, env, timeout):
        out = {"is_error": False, "subtype": "success", "result": "done", "num_turns": 1,
               "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 5}}
        return Proc(0, json.dumps(out), "")
    rec = _rec("T3", "success")
    e = shadow.run_one(tmp_path, rec, "fix typo", "T1", load_config(tmp_path), budget=0.5,
                       test_command=None, runner=runner, worktree=False)
    assert e["outcome"] == "success" and e["verified"] is False and e["shadow_tier"] == "T1"
    s = shadow.summarize([e])
    assert s["n"] == 1 and s["ok"] == 1 and s["verified_n"] == 0


def test_tune_proposes_diff_and_edits_text():
    cfg = load_config("/nonexistent")
    recs = [_rec("T1", "failure", trace={"rules": ["flaky-ish"], "hard_terms": []})
            for _ in range(4)]
    ch = tune.propose(recs, cfg)
    kinds = {c["kind"] for c in ch}
    assert "hard_term" in kinds and "class_base" in kinds
    old = "version: 1\nhard_terms: []   # my words\nclass_base:\n  bugfix: T1\n"
    new = tune.apply_text(old, ch)
    assert "flaky-ish" in new and "# my words" in new and "bugfix: T2" in new
    assert any(line.startswith("+") for line in tune.diff(old, new))


def test_tune_cli_does_not_write_without_apply(tmp_path):
    from typer.testing import CliRunner
    from ccr.cli import app
    (tmp_path / ".ccr").mkdir()
    cfgp = tmp_path / ".ccr" / "config.yaml"
    cfgp.write_text("version: 1\n")
    for _ in range(4):
        H.append(tmp_path, _rec("T1", "failure", trace={"rules": ["zork"]}))
    res = CliRunner().invoke(app, ["tune", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "not applied" in res.output
    assert cfgp.read_text() == "version: 1\n"
    res = CliRunner().invoke(app, ["tune", "--apply", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "zork" in cfgp.read_text()
    assert list((tmp_path / ".ccr" / "backups").glob("config.yaml.*"))


def test_shadow_cli_dry_run_needs_budget_and_yes(tmp_path):
    from typer.testing import CliRunner
    from ccr.cli import app
    from ccr.history import prompts
    rec = _rec("T3", "success")
    H.append(tmp_path, rec)
    prompts.append(tmp_path, {"id": rec["id"], "prompt": "do a thing"})
    r1 = CliRunner().invoke(app, ["shadow", "--path", str(tmp_path)])
    assert r1.exit_code == 2 and "--max-budget-usd" in r1.output
    r2 = CliRunner().invoke(app, ["shadow", "--max-budget-usd", "1", "--path", str(tmp_path)])
    assert r2.exit_code == 0 and "dry run" in r2.output and not shadow.read(tmp_path)


def test_manual_model_up_signal_needs_same_code_or_follow_up():
    from types import SimpleNamespace as NS
    from ccr.hooks.prompt import escalation_signals
    cfg = load_config("/nonexistent")
    d = NS(location=NS(candidates=[NS(path="a.py")], located=True), area="app")
    pend = {"main_model": "sonnet", "area": "app", "ts": time.time()}
    sess = {"files": ["a.py"], "task": {}}
    assert escalation_signals("add a docs page", pend, sess, d, "claude-opus-4", cfg) == \
        ["manual_model_up"]
    other = NS(location=NS(candidates=[NS(path="z.py")], located=True), area="other")
    assert escalation_signals("write a long unrelated new feature spec for billing exports now",
                              pend, {"files": ["a.py"], "task": {}}, other, "claude-opus-4",
                              cfg) == []
    assert escalation_signals("add a docs page", pend, sess, d, "claude-haiku-4", cfg) == []

"""History correctness (dedup, ordering, corrupt lines, feedback resolution, merged files),
`ccr stats` numbers, `ccr feedback`, and the learning loop (up / down / explore)."""
import json
import shutil

import pytest
from typer.testing import CliRunner

from ccr import history as H
from ccr.cli import app
from ccr.config import default_config
from ccr.graph import Graph
from ccr.history import learn
from ccr.history.stats import compute, format_text
from ccr.route import decide

cli = CliRunner()


def rec(kind="bugfix", area="app/api", tier="T1", outcome="success", ts=1000, **kw):
    return H.make_record(None, outcome, **{"class": kind, "area": area, "tier": tier,
                                           "ts": ts, **kw})


def write_lines(path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((x if isinstance(x, str) else json.dumps(x)) + "\n"
                            for x in items))


# ----------------------------------------------------------------------- history correctness
def test_records_get_ids_and_whitelist(tmp_path):
    r = rec(prompt="secret text", task="secret")
    assert len(r["id"]) == 16 and "prompt" not in r and "task" not in r
    H.append(tmp_path, {**r, "prompt": "secret text"})
    assert "secret" not in H.history_path(tmp_path).read_text()
    assert H.read(tmp_path) == [r]


def test_corrupt_duplicate_and_out_of_order_lines(tmp_path):
    a, b, c = rec(ts=300), rec(ts=100, tier="T2"), rec(ts=200, outcome="failure")
    old = {"class": "tests", "tier": "T1", "outcome": "success", "ts": 50}   # pre-id record
    write_lines(H.history_path(tmp_path), [
        a, "not json", b, "[1, 2]", "", '{"broken": ', c, a, old, old, b, "<<<<<<< HEAD",
        "=======", ">>>>>>> branch", '"just a string"', "\x00\x01garbage"])
    out = H.read(tmp_path)
    assert [r["ts"] for r in out] == [50, 100, 200, 300]
    assert [r.get("id") for r in out] == [None, b["id"], c["id"], a["id"]]


def test_feedback_resolution_newest_wins_regardless_of_position(tmp_path):
    a = rec(ts=100, outcome="success")
    fb_old = H.feedback_record(a["id"], "bad", now=200)
    fb_new = H.feedback_record(a["id"], "good", now=300)
    # newest feedback appears first in the file, and before the record it corrects
    write_lines(H.history_path(tmp_path), [fb_new, fb_old, a, fb_old])
    (r,) = H.read(tmp_path)
    assert r["outcome"] == "success" and r["feedback"] == "good"
    write_lines(H.history_path(tmp_path), [fb_new, a, H.feedback_record(a["id"], "bad",
                                                                         now=400)])
    (r,) = H.read(tmp_path)
    assert r["outcome"] == "failure" and r["feedback"] == "bad"
    # feedback for an unknown id and malformed feedback are ignored
    write_lines(H.history_path(tmp_path), [a, H.feedback_record("nope", "bad"),
                                           {"type": "feedback", "ref": a["id"],
                                            "outcome": "maybe"}])
    assert H.read(tmp_path)[0]["outcome"] == "success"


def test_merged_branches_concatenated_count_once(tmp_path):
    """Two branches appended to a common history; a union merge (or a hand-resolved
    conflict) concatenates both sides, duplicating the shared prefix in any order."""
    base = [rec(ts=i, outcome="success") for i in range(1, 6)]
    mine = [rec(ts=10, outcome="failure"), rec(ts=12, tier="T2")]
    theirs = [rec(ts=11, kind="feature", area="app/reports", tier="T2"),
              H.feedback_record(base[0]["id"], "bad", now=13)]
    merged = [*base, *mine, "<<<<<<< ours", "=======", *base[::-1], *theirs,
              ">>>>>>> theirs", *mine]
    write_lines(H.history_path(tmp_path), merged)
    out = H.read(tmp_path)
    assert len(out) == 8
    assert [r["ts"] for r in out] == [1, 2, 3, 4, 5, 10, 11, 12]
    assert out[0]["outcome"] == "failure" and out[0]["feedback"] == "bad"
    st = compute(out, default_config())
    assert st["tasks"] == 8 and st["tiers"]["T1"]["failure"] == 2


def test_window_and_missing_file(tmp_path):
    assert H.read(tmp_path) == []
    write_lines(H.history_path(tmp_path), [rec(ts=100), rec(ts=200_000)])
    assert len(H.read(tmp_path, window_days=1, now=200_000 + 3600)) == 1


# ----------------------------------------------------------------------- stats numbers
def sample():
    return [
        rec("bugfix", "app/api", "T1", "failure", 1, escalated_to="T2", source="run",
            run_id="r1", tokens=1000, cost_usd=0.02),
        rec("bugfix", "app/api", "T2", "success", 2, escalated_from="T1", source="run",
            run_id="r1", tokens=3000, cost_usd=0.10),
        rec("bugfix", "app/api", "T1", "success", 3, source="hook", tokens=500),
        rec("feature", "app/reports", "T2", "success", 4, source="hook", tokens=4000),
        rec("feature", "app/reports", "T2", None, 5, source="hook", tokens=100),
        rec("question", "*", "T0", "success", 6, source="hook", tokens=50,
            assist_cost_usd=0.001),
    ]


def test_stats_numbers():
    s = compute(sample(), default_config())
    assert s["tasks"] == 6 and s["runs"] == 1 and (s["since"], s["until"]) == (1, 6)
    assert s["usage"]["tokens"] == 8650
    assert s["usage"]["cost_usd"] == pytest.approx(0.121)
    assert s["usage"]["by_source"]["run"] == {"tasks": 2, "tokens": 4000, "cost_usd": 0.12}
    assert s["usage"]["by_source"]["hook"]["tasks"] == 4
    o = s["overall"]
    assert (o["success"], o["failure"], o["unknown"], o["escalated"]) == (4, 1, 1, 1)
    assert o["success_rate"] == 0.8 and o["escalation_rate"] == pytest.approx(1 / 6, abs=1e-3)
    t = s["tiers"]
    assert {k: v["tasks"] for k, v in t.items()} == {"T0": 1, "T1": 2, "T2": 3}
    assert t["T1"]["share"] == pytest.approx(0.333) and t["T2"]["share"] == 0.5
    assert t["T1"]["success_rate"] == 0.5 and t["T1"]["escalation_rate"] == 0.5
    assert t["T2"]["success_rate"] == 1.0 and t["T2"]["unknown"] == 1
    assert t["T2"]["escalation_rate"] == 0.0
    api = s["areas"]["app/api"]
    assert api["tasks"] == 3 and api["tiers"] == {"T1": 2, "T2": 1}
    assert api["success_rate"] == pytest.approx(0.667, abs=1e-3)
    assert api["escalation_rate"] == pytest.approx(0.333, abs=1e-3)
    assert s["classes"]["feature"]["success_rate"] == 1.0
    assert s["classes"]["question"]["cost_usd"] == pytest.approx(0.001)


def test_stats_text_and_cli(tmp_path):
    for r in sample():
        H.append(tmp_path, r)
    lines = format_text(compute(H.read(tmp_path), default_config()), when=str)
    text = "\n".join(lines)
    assert "Tasks:     6 (hook 4, run 2; 1 ccr run(s))" in text
    assert "Tier distribution: T0 1 (17%)  T1 2 (33%)  T2 3 (50%)" in text
    assert "est. cost $0.1210" in text
    r = cli.invoke(app, ["stats", "--json", "--path", str(tmp_path)])
    assert r.exit_code == 0 and json.loads(r.output)["tasks"] == 6
    r = cli.invoke(app, ["stats", "--path", str(tmp_path)])
    assert r.exit_code == 0 and "Per area:" in r.output and "app/reports" in r.output
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "no routed tasks" in cli.invoke(app, ["stats", "--path", str(empty)]).output


# ----------------------------------------------------------------------- learning rules
def _hist(kind, area, tier, ok, bad):
    return ([{"class": kind, "area": area, "tier": tier, "outcome": "success"}] * ok +
            [{"class": kind, "area": area, "tier": tier, "outcome": "failure"}] * bad)


def adj(tier, records, **kw):
    kw.setdefault("cap", 3)
    kw.setdefault("floor", 1)
    return learn.adjust(tier, kind="bugfix", area="app/api", records=records,
                        cfg=kw.pop("cfg", default_config()), **kw)


def test_learn_up_on_failure_rate():
    r = adj(1, _hist("bugfix", "app/api", "T1", 1, 3))
    assert (r.tier, r.delta) == (2, 1) and r.note == "T1 failed 3/4 for bugfix in app/api: +1"
    r = adj(1, _hist("bugfix", "app/api", "T1", 1, 1))          # below min_samples
    assert r.delta == 0 and "T1 1/2 ok" in r.note
    r = adj(2, _hist("bugfix", "app/api", "T2", 0, 3), cap=2)   # cap
    assert r.delta == 0 and "cap reached" in r.note
    cfg = default_config()
    cfg.data["history"].update(min_samples=10)
    assert adj(1, _hist("bugfix", "app/api", "T1", 0, 9), cfg=cfg).delta == 0


def test_learn_down_when_lower_tier_proven():
    r = adj(2, _hist("bugfix", "app/api", "T1", 4, 1))
    assert (r.tier, r.delta) == (1, -1) and "T1 succeeded 4/5" in r.note
    assert adj(2, _hist("bugfix", "app/api", "T1", 3, 1)).delta == 0     # 75% < 80%
    assert adj(2, _hist("bugfix", "app/api", "T1", 5, 0), critical=True).delta == 0
    assert adj(1, _hist("bugfix", "app/api", "T0", 5, 0)).delta == 0     # floor T1


def test_learn_explore_down_on_low_risk_simple_tasks():
    hist = _hist("bugfix", "app/api", "T2", 5, 0)
    r = adj(2, hist, low_risk_simple=True)
    assert (r.tier, r.delta) == (1, -1)
    assert r.reason == ("-1: history: T2 succeeded 5/5 for bugfix in app/api, low-risk "
                        "simple task: trying T1")
    assert adj(2, hist, low_risk_simple=False).delta == 0            # not low-risk/simple
    assert adj(2, hist + _hist("bugfix", "app/api", "T1", 0, 1),
               low_risk_simple=True).delta == 0                       # T1 already failed
    r = adj(2, hist + _hist("bugfix", "app/api", "T1", 0, 3), low_risk_simple=True)
    assert r.delta == 0 and "T1 failed 3/3, staying" in r.note
    cfg = default_config()
    cfg.data["history"]["explore_down"] = False
    assert adj(2, hist, low_risk_simple=True, cfg=cfg).delta == 0
    # after exploring, failures at the lower tier send it back up: the loop settles
    r = adj(1, hist + _hist("bugfix", "app/api", "T1", 1, 2), low_risk_simple=True)
    assert (r.tier, r.delta) == (2, 1)


def test_learn_area_then_class_scope():
    r = adj(1, _hist("bugfix", "elsewhere", "T1", 0, 3))
    assert r.delta == 1 and "bugfix (all areas)" in r.note


def test_summarize_explains_with_counts():
    recs = (_hist("bugfix", "app/api", "T1", 1, 3) + _hist("feature", "app/ui", "T2", 6, 0)
            + _hist("tests", "app", "T1", 1, 0))
    notes = {(x["class"], x["action"]): x["note"] for x in learn.summarize(recs,
                                                                          default_config())}
    assert notes[("bugfix", "up")] == "bugfix in app/api: T1 failed 3/4 -> start at T2"
    assert notes[("feature", "explore")] == ("feature in app/ui: T2 ok 6/6 -> low-risk "
                                             "simple tasks try T1")
    assert "needs 3 samples" in notes[("tests", "watch")]


# ----------------------------------------------------------------------- router integration
PAGINATE = "Fix the off-by-one error in paginate() in app/api/pagination.py"


def _route(route_env, records, cfg=None):
    root, memory = route_env["shop_py"]
    with Graph.open(root) as g:
        return decide(PAGINATE, root, cfg=cfg or default_config(), graph=g, memory=memory,
                      history_records=records, now=0)


def test_router_explores_down_for_low_risk_simple_task(route_env):
    cfg = default_config()
    cfg.data["class_base"]["bugfix"] = "T2"
    d = _route(route_env, [], cfg)
    assert d.tier.name == "T2" and d.risk.level == "low"
    d = _route(route_env, _hist("bugfix", "app/api", "T2", 5, 0), cfg)
    assert d.tier.name == "T1"
    assert "T2 succeeded 5/5 for bugfix in app/api; low-risk simple task: trying T1" \
        in d.history
    d = _route(route_env, _hist("bugfix", "app/api", "T2", 5, 0)
               + _hist("bugfix", "app/api", "T1", 0, 3), cfg)
    assert d.tier.name == "T2" and "T1 failed 3/3, staying" in d.history


def test_explain_shows_learning_line(route_env):
    root, _ = route_env["shop_py"]
    r = cli.invoke(app, ["explain", PAGINATE, "--path", str(root)])
    assert r.exit_code == 0 and "Learning:  no history for bugfix" in r.output


# ----------------------------------------------------------------------- feedback
def test_feedback_on_run_record_changes_learning(tmp_path):
    for i in range(3):
        H.append(tmp_path, rec(ts=100 + i, outcome="success", source="run"))
    assert learn.adjust(1, kind="bugfix", area="app/api", records=H.read(tmp_path),
                        cfg=default_config(), cap=2, floor=1).delta == 0
    r = cli.invoke(app, ["feedback", "bad", "--last", "--path", str(tmp_path)])
    assert r.exit_code == 0 and "marked bad (failure)" in r.output and "ccr run" in r.output
    recs = H.read(tmp_path)
    assert [x["outcome"] for x in recs] == ["success", "success", "failure"]
    ids = [x["id"] for x in recs]
    cli.invoke(app, ["feedback", "bad", "--id", ids[1][:6], "--path", str(tmp_path)])
    recs = H.read(tmp_path)
    assert [x["outcome"] for x in recs] == ["success", "failure", "failure"]
    a = learn.adjust(1, kind="bugfix", area="app/api", records=recs, cfg=default_config(),
                     cap=2, floor=1)
    assert a.delta == 1 and "T1 failed 2/3" in a.note
    # good flips it back; the raw file only ever grew
    cli.invoke(app, ["feedback", "good", "--path", str(tmp_path)])
    assert H.read(tmp_path)[-1]["outcome"] == "success"
    assert len(H.read_raw(tmp_path)) == 6
    r = cli.invoke(app, ["feedback", "--path", str(tmp_path)])
    assert "feedback good" in r.output
    assert cli.invoke(app, ["feedback", "meh", "--path", str(tmp_path)]).exit_code == 2
    empty = tmp_path / "e"
    empty.mkdir()
    assert cli.invoke(app, ["feedback", "bad", "--path", str(empty)]).exit_code == 1


@pytest.fixture
def hook_proj(route_env, tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(route_env["shop_py"][0], root)
    return root


def test_feedback_on_pending_hook_task(hook_proj):
    from test_hooks import prompt
    root = hook_proj
    assert prompt(root, PAGINATE)
    sess = json.loads((root / ".ccr" / "local" / "sessions" / "s1.json").read_text())
    pid = sess["pending"]["id"]
    r = cli.invoke(app, ["feedback", "bad", "--path", str(root)])
    assert r.exit_code == 0 and "in progress" in r.output and pid[:8] in r.output
    # the user moves on: the hook closes the task as a success, feedback still wins
    prompt(root, "Add a CSV export option to app/reports/export.py")
    (first,) = [x for x in H.read(root) if x["id"] == pid]
    assert first["outcome"] == "failure" and first["feedback"] == "bad"
    assert first["source"] == "hook"


def test_hook_retry_records_escalated_to(hook_proj):
    from test_hooks import prompt
    first = prompt(hook_proj, "add a test for the pagination helper")
    tier = first.split()[1]
    prompt(hook_proj, "still failing, same error")
    (r,) = H.read(hook_proj)
    assert r["outcome"] == "failure" and r["escalated_to"] == f"T{int(tier[1]) + 1}"
    assert compute(H.read(hook_proj), default_config())["tiers"][tier]["escalated"] == 1


def test_doctor_reports_history_health(tmp_path):
    a = rec(ts=1)
    write_lines(H.history_path(tmp_path), [a, a, "<<<<<<< HEAD", H.feedback_record(a["id"],
                                                                                   "bad")])
    from ccr.cli import _doctor_history
    (msg,) = [m for _, m in _doctor_history(tmp_path)]
    assert msg == ("history: 1 task(s), 1 feedback record(s), 1 duplicate line(s) ignored, "
                   "1 unreadable line(s) skipped")


# ------------------------------------------------------------ savings and misroutes
from ccr.history import savings  # noqa: E402


def _t(tier, outcome, kind="bugfix", area="app/api", tokens=1000, **kw):
    return {"id": f"{tier}{outcome}{kind}{area}{kw}", "class": kind, "area": area,
            "tier": tier, "outcome": outcome, "tokens": tokens, **kw}


def test_savings_estimate_prices_tokens_per_tier_against_baselines():
    cfg = default_config()
    est = savings.estimate([_t("T0", "success", tokens=1_000_000),
                            _t("T3", "success", tokens=1_000_000)], cfg)
    assert est["routed_usd"] == pytest.approx(2.0 + 30.0)
    assert est["all_opus_usd"] == 60.0 and est["saved_vs_opus"] == pytest.approx(0.467, abs=1e-3)
    assert est["all_sonnet_usd"] == 12.0 and est["saved_vs_sonnet"] < 0   # honest: costlier
    assert savings.estimate([_t("T1", "success", tokens=0)], cfg)["saved_vs_opus"] is None


def test_pricing_and_hard_terms_are_configurable(tmp_path):
    from ccr.config import load_config
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text(
        "pricing:\n  opus: 50\n  gpt: 1\nhard_terms: [Cache Warm]\n")
    cfg = load_config(tmp_path)
    assert cfg["pricing"]["opus"] == 50.0 and cfg["hard_terms"] == ["cache warm"]
    assert any("pricing.gpt" in w for w in cfg.warnings)


def test_overspend_needs_a_proven_cheaper_tier():
    recs = [_t("T1", "success", kind="feature", id=i) for i in range(3)]   # T1 proven
    recs.append(_t("T2", "success", kind="feature", id="x"))
    m = savings.misroutes(recs, default_config())
    assert m["over"] == 1 and m["under"] == 0 and m["decided"] == 4
    assert m["items"][0]["kind"] == "over" and "T1 succeeded 3/3" in m["items"][0]["why"]
    # one lucky cheap success is not proof
    assert savings.misroutes([_t("T1", "success", kind="feature"),
                              _t("T2", "success", kind="feature", id="y")],
                             default_config())["over"] == 0
    # nothing is "over" at the class floor
    assert savings.misroutes([_t("T1", "success", id=i) for i in range(5)],
                             default_config())["over"] == 0


def test_underrouted_failures_become_tuning_suggestions():
    trace = {"rules": [], "hard": False, "bumps": [], "class_conf": 0.15}
    recs = [_t("T1", "failure", id=i, trace=trace) for i in range(2)]
    recs.append(_t("T1", "escalated", id="e", trace={**trace, "rules": ["fix"]}))
    m = savings.misroutes(recs, default_config())
    assert m["under"] == 3 and m["under_rate"] == 1.0
    text = " | ".join(m["suggestions"])
    assert "2/3 bugfix task(s) failed at T1 with no cue word" in text
    assert "hard_terms" in text
    old = savings.misroutes([_t("T1", "failure")], default_config())
    assert "no routing trace" in old["suggestions"][0]


def test_stats_reports_savings_and_misroutes_in_text_and_cli(tmp_path):
    for r in [*(_t("T1", "success", kind="feature", id=i) for i in range(3)),
              _t("T2", "success", kind="feature", id="x"),
              _t("T1", "failure", id="f", trace={"rules": [], "hard": False, "bumps": []})]:
        H.append(tmp_path, {**r, "ts": 1})
    out = cli.invoke(app, ["stats", "--path", str(tmp_path)]).output
    assert "Savings:" in out and "Misroutes: over-routed 1/5" in out and "tune:" in out
    detail = cli.invoke(app, ["stats", "--misroutes", "--path", str(tmp_path)]).output
    assert " over  T2 feature" in detail and " under T1 bugfix" in detail
    js = json.loads(cli.invoke(app, ["stats", "--json", "--path", str(tmp_path)]).output)
    assert js["misroutes"]["over"] == 1 and js["savings"]["tasks"] == 5


def test_bench_routing_replay_has_no_underrouted_prompts():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "scripts" / "bench_routing.py"
    spec = importlib.util.spec_from_file_location("bench_routing", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.replay(mod.setup())
    assert out["under"] == 0 and out["over"] == 0, [r for r in out["rows"]
                                                    if r["under"] or r["over"]]
    assert out["estimate"]["saved_vs_opus"] > 0.2


def test_cost_per_success_charges_failures_to_the_routed_side():
    cfg = default_config()
    recs = [_t("T1", "success", tokens=1_000_000, id="a"),
            _t("T1", "failure", tokens=1_000_000, id="b")]
    est = savings.estimate(recs, cfg)
    assert est["routed_per_success"] == pytest.approx(12.0)     # 2 x $6 over 1 success
    assert est["opus_per_success"] == pytest.approx(30.0)       # Opus assumed to always pass
    assert est["saved_per_success"] == pytest.approx(0.6)
    bad = savings.estimate([_t("T3", "failure", tokens=1000, id=i) for i in range(2)], cfg)
    assert bad["routed_per_success"] is None and bad["saved_per_success"] is None
    lines = savings.format_lines(est, savings.misroutes(recs, cfg))
    assert any("per success" in ln for ln in lines)


def test_cue_that_keeps_failing_is_flagged_as_drifted():
    ok = {"rules": ["fix"], "hard_terms": []}
    bad = {"rules": ["fix", "cache"], "hard_terms": ["cache"]}
    recs = [_t("T1", "success", id=f"s{i}", trace=ok) for i in range(2)]
    recs += [_t("T1", "failure", id=f"f{i}", trace=bad) for i in range(3)]
    m = savings.misroutes(recs, default_config())
    flagged = {c["term"] for c in m["cues"]}
    assert flagged == {"cache", "fix"}       # fix: 3 of its 5 tasks failed too
    assert any("cue 'cache' failed 3/3 at T1" in s for s in m["suggestions"])


def test_bench_accepts_a_custom_prompt_file(tmp_path):
    import importlib.util
    import json as _json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "scripts" / "bench_routing.py"
    spec = importlib.util.spec_from_file_location("bench_routing2", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    f = tmp_path / "p.json"
    f.write_text(_json.dumps([["where is the pagination helper defined?", "T0", "T1"],
                              ["fix login sometimes failing", "T4", "T4"]]))
    out = mod.replay(mod.setup(), prompts=mod.load_prompts(str(f)))
    assert out["total"] == 2 and out["under"] == 1      # the file's own labels decide


def test_struggled_success_counts_as_failure_for_learning():
    from ccr.config import load_config
    from ccr.history import learn, make_record
    cfg = load_config("/nonexistent")
    recs = [make_record({"tier": "T1", "class": "bugfix", "area": "a"}, outcome="success",
                        tool_errors=9) for _ in range(3)]
    res = learn.adjust(1, kind="bugfix", area="a", records=recs, cfg=cfg, cap=3, floor=1)
    assert res.delta == 1 and "failed 3/3" in res.note
    assert not learn.struggled({"outcome": "failure", "tool_errors": 99}, cfg["history"])
    cfg["history"]["struggle_tool_errors"] = 0
    assert learn.adjust(1, kind="bugfix", area="a", records=recs, cfg=cfg, cap=3, floor=1).delta <= 0

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

import random

from ccr.history import calibrate, make_record


def _rec(tier, outcome, **trace):
    t = {"base": tier, "scope": "medium", "vagueness": 0.2, "class_conf": 0.8, "risk_level": "low"}
    t.update(trace)
    return make_record({"tier": tier, "class": "bugfix", "area": "a", "trace": t}, outcome=outcome)


def test_not_ready_with_little_data():
    res = calibrate.fit([_rec("T1", "success")])
    assert not res["ready"] and "need at least" in res["note"]


def test_learns_that_hard_tasks_fail_at_t1():
    rnd = random.Random(1)
    recs = []
    for _ in range(80):
        hard = rnd.random() < 0.5
        recs.append(_rec("T1", "failure" if hard else "success", hard=hard,
                         risk_level="high" if hard else "low"))
    res = calibrate.fit(recs)
    assert res["ready"] and res["cv_auc"] > 0.9 and res["useful"]
    assert res["shadow"]["stronger"] > 0


def test_export_labels(tmp_path):
    bad, good = _rec("T1", "failure"), _rec("T2", "success")
    items = calibrate.export_benchmark(
        [bad, good], {bad["id"]: {"prompt": "p1"}, good["id"]: {"prompt": "p2"}})
    assert items[0] == ["p1", "T2", "T4"]
    assert items[1][0] == "p2" and items[1][2] == "T2H"

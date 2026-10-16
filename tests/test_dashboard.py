import json

from ccr.config import load_config
from ccr.history import dashboard, make_record


def _records():
    return [make_record({"tier": "T1", "class": "bugfix", "area": "app"}, outcome="success", tokens=1000),
            make_record({"tier": "T3", "class": "debugging", "area": "app"}, outcome="failure", tokens=5000)]


def test_payload_has_rows_without_prompt_text(tmp_path):
    p = dashboard.build_payload(_records(), load_config(tmp_path), tmp_path)
    assert [r["result"] for r in p["task_rows"]] == ["ok", "fail"]
    assert all(r["decision"] is None for r in p["task_rows"])
    assert '"prompt":' not in json.dumps(p)
    assert p["tier_order"][0] == "T0"


def test_write_self_contained_html(tmp_path):
    out = dashboard.write(tmp_path, _records(), load_config(tmp_path), tmp_path / "o" / "d.html")
    html = out.read_text()
    assert "ccr Dashboard" in html and "__DATA__" not in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")


def test_empty_history_renders(tmp_path):
    out = dashboard.write(tmp_path, [], load_config(tmp_path), tmp_path / "d.html")
    assert out.exists()


def test_prompt_log_joins_into_rows(tmp_path):
    from ccr.history import prompts
    from ccr.route import decide
    cfg = load_config(tmp_path)
    d = decide("fix the typo in the README", tmp_path, cfg=cfg)
    rec = make_record(d, "success", tokens=10)
    prompts.append(tmp_path, prompts.snapshot(rec["id"], "fix the typo in the README", d, d.tier, "f"))
    p = dashboard.build_payload([rec], cfg, tmp_path)
    row = p["task_rows"][0]
    assert row["decision"]["prompt"] == "fix the typo in the README"
    assert row["decision"]["reasons"] and row["trace"]


def test_prompts_off_by_default(tmp_path):
    assert load_config(tmp_path)["hooks"]["store_prompts"] is False


def test_why_checklist_from_decision_and_trace(tmp_path):
    from ccr.history import prompts, why
    from ccr.route import decide
    cfg = load_config(tmp_path)
    d = decide("fix the bug where login sometimes fails", tmp_path, cfg=cfg)
    w = why.from_decision(d, d.tier)
    assert [c["n"] for c in w["checks"]] == list(range(1, 10))
    assert w["chain"][0].startswith("Start at") and w["chain"][-1].startswith("Final")
    assert any(c["move"] == "up" for c in w["checks"])
    snap = prompts.snapshot("id1", "p", d, d.tier, "f")
    assert snap["why"]["checks"]
    rec = make_record(d, "success", tokens=1)
    row = dashboard.build_payload([rec], cfg, tmp_path)["task_rows"][0]
    assert row["why"]["partial"] and row["why"]["checks"][0]["title"].startswith("What kind")

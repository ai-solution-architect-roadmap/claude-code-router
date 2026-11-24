"""Code-first routing (`code_first.enabled`): files from the real repository state, tier from
the code score, keywords only break ties. Off by default."""
import shutil

import pytest
from conftest import ROUTE_REPOS, git_commit_all

from ccr.config import default_config, load_config
from ccr.evaluate.locate import locate
from ccr.evaluate.workspace import snapshot
from ccr.graph import Graph, update
from ccr.route import decide
from ccr.route.budget import Budget
from ccr.route.code import code_tier


def _cfg(on=True, **extra):
    cfg = default_config()
    cfg.data["code_first"]["enabled"] = on
    cfg.data["code_first"].update(extra)
    return cfg


def _decide(root, task, cfg, memory=None):
    g = Graph.open(root)
    try:
        return decide(task, root, cfg=cfg, graph=g, memory=memory, budget=Budget(),
                      history_records=[])
    finally:
        if g is not None:
            g.close()


@pytest.fixture
def shop(tmp_path):
    """A private copy of the Python fixture repo (git + graph) that a test may edit."""
    root = tmp_path / "shop"
    shutil.copytree(ROUTE_REPOS["shop_py"], root)
    git_commit_all(root)
    update(root, workers=1, offline=True)
    return root


# --------------------------------------------------------------------------- config

def test_off_by_default():
    assert default_config()["code_first"]["enabled"] is False


def test_config_validates_code_first(tmp_path):
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text(
        "code_first:\n  enabled: true\n  recent_commits: 3\n  bands: [0.1, 0.3, 0.5]\n")
    cfg = load_config(tmp_path)
    assert not cfg.errors
    assert cfg["code_first"]["enabled"] is True
    assert cfg["code_first"]["recent_commits"] == 3
    assert cfg["code_first"]["bands"] == [0.1, 0.3, 0.5]


@pytest.mark.parametrize("body,needle", [
    ("enabled: yes-please", "code_first.enabled"),
    ("bands: [0.5, 0.3, 0.6]", "code_first.bands"),
    ("bands: [0.2, 0.4]", "code_first.bands"),
    ("min_confidence: 2", "code_first.min_confidence"),
])
def test_config_rejects_bad_code_first(tmp_path, body, needle):
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text(f"code_first:\n  {body}\n")
    cfg = load_config(tmp_path)
    assert any(needle in e for e in cfg.errors), cfg.errors
    assert cfg["code_first"] == default_config()["code_first"]


# --------------------------------------------------------------------------- workspace

def test_snapshot_sees_edits_new_files_and_recent_commits(shop):
    (shop / "app" / "api" / "pagination.py").write_text(
        (shop / "app" / "api" / "pagination.py").read_text() + "\n# a\n# b\n")
    (shop / "app" / "api" / "filters.py").write_text("def f():\n    return 1\n")
    ws = snapshot(shop)
    assert ws.available
    assert ws.dirty["app/api/pagination.py"] == 3          # blank line + 2 comments
    assert "app/api/filters.py" in ws.untracked and ws.dirty["app/api/filters.py"] == 2
    assert "app/core/engine.py" in ws.recent               # from the initial commit


def test_snapshot_outside_git_is_empty(tmp_path):
    ws = snapshot(tmp_path)
    assert not ws.available and not ws.dirty and not ws.recent


def test_unanchored_prompt_finds_the_files_being_edited(shop):
    p = shop / "app" / "reports" / "export.py"
    p.write_text(p.read_text() + "\n\ndef to_csv(rows):\n    return ''\n")
    g = Graph.open(shop)
    try:
        before = locate("finish this and make it work", shop, g)
        after = locate("finish this and make it work", shop, g, workspace=snapshot(shop))
    finally:
        g.close()
    assert not before.located
    assert after.candidates[0].path == "app/reports/export.py"
    assert after.workspace == ["app/reports/export.py"]
    assert after.located


def test_edit_strengthens_a_matched_file(shop):
    p = shop / "app" / "utils" / "strings.py"
    p.write_text(p.read_text() + "\n# wip\n")
    g = Graph.open(shop)
    try:
        task = "the title helper drops a word"
        plain = {c.path: c.confidence for c in locate(task, shop, g).candidates}
        loc = locate(task, shop, g, workspace=snapshot(shop))
    finally:
        g.close()
    c = next(c for c in loc.candidates if c.path == "app/utils/strings.py")
    assert c.confidence > plain.get("app/utils/strings.py", 0.0)
    assert any("uncommitted edit" in r for r in c.reasons)


# --------------------------------------------------------------------------- routing

def test_code_first_off_matches_keyword_routing(route_env):
    root, mem = route_env["shop_py"]
    task = "Add a CSV export option to app/reports/export.py"
    a = _decide(root, task, default_config(), mem)
    b = _decide(root, task, _cfg(False), mem)
    assert a.tier.name == b.tier.name and a.reasons == b.reasons and b.code is None


@pytest.mark.parametrize("task,tier", [
    ("Fix the typo in the docstring of slugify in app/utils/strings.py", "T0"),
    ("Fix the off-by-one error in paginate() in app/api/pagination.py; the last page is "
     "dropped", "T1"),
    ("Refactor Engine.run into smaller functions without changing behaviour", "T3"),
    ("Fix the bug where refunds in the payment ledger are double counted", "T4"),
])
def test_code_sets_the_tier(route_env, task, tier):
    root, mem = route_env["shop_py"]
    d = _decide(root, task, _cfg(), mem)
    assert d.code["applies"], d.reasons
    assert d.tier.name == tier, d.reasons
    assert any(r.startswith("code-first: code score") for r in d.reasons)


@pytest.mark.parametrize("task,why", [
    ("What does Engine.run do?", "does not edit code"),
    ("something is broken, fix it", "files not known"),
])
def test_falls_back_to_keywords(route_env, task, why):
    root, mem = route_env["shop_py"]
    d = _decide(root, task, _cfg(), mem)
    off = _decide(root, task, default_config(), mem)
    assert not d.code["applies"] and why in d.code["fallback"]
    assert d.tier.name == off.tier.name
    assert any(r.startswith("code-first: fell back") for r in d.reasons)


def test_falls_back_without_a_graph(tmp_path):
    root = tmp_path / "nograph"
    shutil.copytree(ROUTE_REPOS["shop_py"], root)
    d = decide("Add a CSV export option to app/reports/export.py", root, cfg=_cfg(),
               graph=None, memory=None, budget=Budget(), history_records=[])
    assert not d.code["applies"] and "no code graph" in d.code["fallback"]


def test_a_large_change_in_progress_raises_the_tier(shop):
    task = "Add a CSV export option to app/reports/export.py"
    clean = _decide(shop, task, _cfg())
    p = shop / "app" / "reports" / "export.py"
    p.write_text(p.read_text() + "".join(f"\nx_{i} = {i}" for i in range(300)))
    busy = _decide(shop, task, _cfg())
    assert busy.code["lines"] >= 300 and busy.code["parts"]["change_size"] > 0.9
    assert busy.code["score"] > clean.code["score"]
    assert busy.tier.index > clean.tier.index, (clean.reasons, busy.reasons)


def test_class_floor_keeps_room_to_think(route_env):
    root, mem = route_env["svc_go"]
    d = _decide(root, "Find out why memory usage keeps growing in the server", _cfg(), mem)
    assert d.task_class.kind == "debugging" and d.code["applies"]
    assert d.tier.name == "T2H", d.reasons         # one below the T3 start, never lower


def test_keywords_break_a_tie(route_env):
    root, mem = route_env["shop_py"]
    task = "Fix the off-by-one error in paginate() in app/api/pagination.py; the last page is dropped"
    d = _decide(root, task, _cfg(), mem)
    s = d.code["score"]
    # an edge just under the score would put it in T2; bugfix starts at T1, so the tie goes
    # to the lower side. Without the margin the code alone decides.
    edges = [round(s - 0.01, 3), 0.7, 0.9]
    tied = _decide(root, task, _cfg(bands=edges, tie_margin=0.05), mem)
    assert tied.tier.name == "T1", tied.reasons
    assert any("breaks the tie" in r for r in tied.reasons)
    strict = _decide(root, task, _cfg(bands=edges, tie_margin=0.0), mem)
    assert strict.tier.name == "T2", strict.reasons


def test_decision_reports_code_first(route_env):
    root, mem = route_env["shop_py"]
    d = _decide(root, "Add a CSV export option to app/reports/export.py", _cfg(), mem)
    out = d.to_dict()["code_first"]
    assert out["applies"] and out["tier"] and out["files"] == ["app/reports/export.py"]
    tr = d.trace()
    assert tr["code_first"] is True and isinstance(tr["code_score"], float)
    assert "app/" not in str(tr)                      # trace stays free of file paths


def test_code_tier_ignores_non_edits(route_env):
    root, mem = route_env["shop_py"]
    d = _decide(root, "What does Engine.run do?", _cfg(), mem)
    ct = code_tier(d.task_class, d.location, d.risk, _cfg())
    assert not ct.applies

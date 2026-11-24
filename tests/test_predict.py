"""Step 2 of code-first routing: the Haiku change prediction (`predict.enabled`). Haiku is
always mocked here: every call goes through one runner."""
import json

import pytest

from ccr.config import TIERS, default_config, load_config
from ccr.evaluate import llm as L
from ccr.evaluate.predict import Prediction, cache_key, parse, verify_files
from ccr.graph import Graph
from ccr.route import decide
from ccr.route.budget import Budget


def _cfg(predict=True):
    cfg = default_config()
    cfg.ask = predict            # read by _decide: allow_predict
    return cfg


def _runner(answer, calls=None):
    def run(prompt, *, model, effort, timeout):
        if calls is not None:
            calls.append(prompt)
        if isinstance(answer, Exception):
            raise answer
        return {"result": json.dumps(answer) if isinstance(answer, dict) else answer,
                "total_cost_usd": 0.0004}
    return run


def _decide(root, task, cfg, runner, mem=None):
    g = Graph.open(root)
    try:
        return decide(task, root, cfg=cfg, graph=g, memory=mem, budget=Budget(),
                      history_records=[], predict_runner=runner,
                      allow_predict=getattr(cfg, "ask", True))
    finally:
        g.close()


@pytest.fixture
def fresh(route_env, tmp_path, monkeypatch):
    """Route env with the prediction cache pointed at a fresh directory per test."""
    from ccr.evaluate import predict as P
    monkeypatch.setattr(P, "_cache_path", lambda root: tmp_path / "cache.json")
    return route_env


SMALL = {"files": [{"path": "internal/cache/cache.go", "confidence": 0.9}], "lines": 8,
         "reach": "one_function", "interface_change": False, "schema_change": False,
         "confidence": 0.8, "reason": "one lock in Get"}
LARGE = {"files": [{"path": "src/components/Button.tsx", "confidence": 0.9}], "lines": 400,
         "reach": "cross_cutting", "interface_change": True, "schema_change": False,
         "confidence": 0.8, "reason": "every caller changes"}


# --------------------------------------------------------------------------- parsing

def test_parse_and_size():
    pr = parse("noise " + json.dumps(SMALL) + " trailing")
    assert pr.reach == "one_function" and pr.lines == 8 and pr.size == "small"
    assert pr.files == [("internal/cache/cache.go", 0.9)]
    assert parse(json.dumps(LARGE)).size == "large"
    medium = dict(SMALL, reach="few_files", lines=80)
    assert parse(json.dumps(medium)).size == "medium"
    assert parse(json.dumps(dict(SMALL, schema_change=True))).size == "large"
    assert parse(json.dumps(dict(SMALL, interface_change=True))).size == "medium"


@pytest.mark.parametrize("text", ["", "no json here", "[1, 2]", '{"confidence": 0.9}', "{bad"])
def test_parse_rejects_unusable_answers(text):
    assert parse(text) is None


def test_verify_drops_invented_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    pr = Prediction(files=[("src/a.py", 0.9), ("src/new_helper.py", 0.6),
                           ("lib/ghost.py", 0.8)], confidence=0.8)
    verify_files(pr, tmp_path, None, ["src/a.py"])
    assert [p for p, _ in pr.files] == ["src/a.py", "src/new_helper.py"]   # new file next door
    assert pr.dropped == ["lib/ghost.py"] and pr.confidence == 0.8
    only_ghosts = Prediction(files=[("lib/ghost.py", 0.8)], confidence=0.8)
    verify_files(only_ghosts, tmp_path, None, ["src/a.py"])
    assert not only_ghosts.files and only_ghosts.confidence == 0.4


def test_config_validates_predict(tmp_path):
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text(
        "predict:\n  enabled: false\n  timeout_s: 3\n  effort: turbo\n")
    cfg = load_config(tmp_path)
    assert cfg["predict"]["timeout_s"] == 3 and "enabled" not in cfg["predict"]
    assert any("predict.effort" in e for e in cfg.errors)
    assert any("predict.enabled: ignored" in w for w in cfg.warnings)
    assert "enabled" not in default_config()["predict"]
    assert default_config()["predict"]["timeout_s"] == 10.0


# --------------------------------------------------------------------------- gating

def test_always_on_by_default(fresh):
    root, mem = fresh["svc_go"]
    calls = []
    d = _decide(root, "Fix the data race in the LRU cache in internal/cache/cache.go",
                default_config(), _runner(SMALL, calls), mem)
    assert len(calls) == 1 and d.predict["asked"]


def test_missing_claude_cli_falls_back_silently(fresh, monkeypatch, tmp_path):
    from ccr.evaluate import predict as P
    monkeypatch.delenv("CCR_NO_PREDICT")
    monkeypatch.setattr(L.shutil, "which", lambda _: None)
    pr, status = P.predict.__wrapped__("fix it", tmp_path, None, ["a.py"], cfg=default_config())
    assert pr is None and "not found" in status
    root, mem = fresh["svc_go"]
    task = "Fix the data race in the LRU cache in internal/cache/cache.go"
    before = _decide(root, task, _cfg(predict=False), None, mem)
    d = _decide(root, task, default_config(), _runner(L.LLMUnavailable("not found")), mem)
    assert d.tier.name == before.tier.name and "size" not in d.predict


@pytest.mark.parametrize("task", [
    "What does Engine.run do?",                                          # question
    "Fix the typo in the docstring of slugify in app/utils/strings.py",  # trivial
    "Fix the bug where refunds in the payment ledger are double counted",  # T4 rule
])
def test_not_asked_when_routing_is_sure_or_not_an_edit(fresh, task):
    root, mem = fresh["shop_py"]
    calls = []
    d = _decide(root, task, _cfg(), _runner(SMALL, calls), mem)
    assert not calls and d.predict["asked"] is False


def test_answers_are_cached(fresh):
    root, mem = fresh["svc_go"]
    task = "Fix the data race in the LRU cache in internal/cache/cache.go"
    calls = []
    a = _decide(root, task, _cfg(), _runner(SMALL, calls), mem)
    b = _decide(root, task, _cfg(), _runner(SMALL, calls), mem)
    assert len(calls) == 1 and a.predict["asked"]
    assert b.predict["status"] == "cached" and b.tier.name == a.tier.name


def test_cache_key_changes_with_the_working_tree():
    from ccr.evaluate.workspace import Workspace
    k1 = cache_key("fix it", ["a.py"], Workspace(dirty={"a.py": 3}), "h1")
    assert k1 != cache_key("fix it", ["a.py"], Workspace(dirty={"a.py": 9}), "h1")
    assert k1 != cache_key("fix it", ["a.py"], Workspace(dirty={"a.py": 3}), "h2")
    assert k1 == cache_key("fix it", ["a.py"], Workspace(dirty={"a.py": 3}), "h1")


# --------------------------------------------------------------------------- effect on the tier

def test_small_change_lowers_a_debugging_task_one_tier(fresh):
    root, mem = fresh["svc_go"]
    task = "Fix the data race in the LRU cache in internal/cache/cache.go"
    before = _decide(root, task, _cfg(predict=False), None, mem)
    d = _decide(root, task, _cfg(), _runner(SMALL), mem)
    assert before.tier.name == "T3"
    assert d.tier.name == "T2H" and d.predict_used, d.reasons
    assert any(r.startswith("change prediction:") and "-1" in r for r in d.reasons)


def test_large_change_raises_one_tier(fresh):
    root, mem = fresh["svc_go"]
    task = "Add a /healthz endpoint to the router in internal/http/router.go"
    before = _decide(root, task, _cfg(predict=False), None, mem)
    large = dict(LARGE, files=[{"path": "internal/http/router.go", "confidence": 0.9}])
    d = _decide(root, task, _cfg(), _runner(large), mem)
    assert d.predict["asked"], d.predict
    assert d.tier.index == before.tier.index + 1 and d.predict_used, d.reasons


def test_large_change_does_not_stack_on_a_code_raise(fresh):
    root, mem = fresh["web_ts"]
    task = "Add an optional icon prop to the Button component"
    d = _decide(root, task, _cfg(), _runner(LARGE), mem)
    assert d.tier.index == TIERS.index(d.code["keyword_tier"]) + 1, d.reasons


def test_small_prediction_cancels_a_code_raise(fresh):
    root, mem = fresh["web_ts"]
    task = "Fix the bug where formatPrice shows NaN for zero amounts"
    code = _decide(root, task, _cfg(predict=False), None, mem)
    small = dict(SMALL, files=[{"path": "src/lib/format.ts", "confidence": 0.9}])
    d = _decide(root, task, _cfg(), _runner(small), mem)
    assert code.tier.index == TIERS.index(code.code["keyword_tier"]) + 1
    assert d.tier.name == code.code["keyword_tier"], d.reasons


def test_low_confidence_prediction_is_reported_not_used(fresh):
    root, mem = fresh["svc_go"]
    task = "Fix the data race in the LRU cache in internal/cache/cache.go"
    d = _decide(root, task, _cfg(), _runner(dict(SMALL, confidence=0.3)), mem)
    assert d.tier.name == "T3" and not d.predict_used
    assert any("not used" in r for r in d.reasons)


def test_perf_work_is_not_lowered(fresh):
    root, mem = fresh["shop_py"]
    task = "Speed up the export in app/reports/export.py, it is slow for large reports"
    small = dict(SMALL, files=[{"path": "app/reports/export.py", "confidence": 0.9}])
    before = _decide(root, task, _cfg(predict=False), None, mem)
    d = _decide(root, task, _cfg(), _runner(small), mem)
    assert d.tier.name == before.tier.name


def test_critical_path_is_not_lowered(fresh):
    root, mem = fresh["svc_go"]
    task = "Update the terraform config to add a new S3 bucket for backups"
    before = _decide(root, task, _cfg(predict=False), None, mem)
    d = _decide(root, task, _cfg(), _runner(dict(SMALL, files=[])), mem)
    assert d.tier.name == before.tier.name
    if d.predict.get("asked"):
        assert any("critical path" in r for r in d.reasons)


@pytest.mark.parametrize("answer", [L.LLMUnavailable("claude timed out after 5s"),
                                    RuntimeError("boom"), "not json"])
def test_failures_leave_routing_unchanged(fresh, answer):
    root, mem = fresh["svc_go"]
    task = "Fix the data race in the LRU cache in internal/cache/cache.go"
    before = _decide(root, task, _cfg(predict=False), None, mem)
    d = _decide(root, task, _cfg(), _runner(answer), mem)
    assert d.tier.name == before.tier.name
    assert d.predict["asked"] and "size" not in d.predict
    assert any("routing unchanged" in n for n in d.notes)


def test_prompt_shows_outlines_not_source(fresh):
    root, mem = fresh["svc_go"]
    calls = []
    _decide(root, "Fix the data race in the LRU cache in internal/cache/cache.go", _cfg(),
            _runner(SMALL, calls), mem)
    sent = calls[0]
    assert "internal/cache/cache.go" in sent and "Uncommitted changes:" in sent
    src = (root / "internal" / "cache" / "cache.go").read_text()
    body = [ln for ln in src.splitlines() if ln.strip().startswith(("if ", "return "))]
    assert body and not any(ln.strip() in sent for ln in body)


def test_trace_has_no_paths(fresh):
    root, mem = fresh["svc_go"]
    d = _decide(root, "Fix the data race in the LRU cache in internal/cache/cache.go",
                _cfg(), _runner(SMALL), mem)
    tr = d.trace()
    assert tr["predict_size"] == "small" and tr["predict_used"] is True
    assert "internal/" not in str(tr)


def test_hook_budget_makes_room_for_the_prediction(tmp_path):
    from ccr.hooks import _body
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text(
        "hooks:\n  route_prompts: false\npredict:\n  timeout_s: 5\n")
    raw = json.dumps({"prompt": "hi", "cwd": str(tmp_path)}).encode()
    result = {}
    _body("prompt", raw, {"CLAUDE_PROJECT_DIR": str(tmp_path)}, result)
    assert result.get("budget") == 6.0        # timeout_s 5 in this config, + 1 s
    result = {}
    _body("stop", raw, {"CLAUDE_PROJECT_DIR": str(tmp_path)}, result)
    assert result.get("budget") == 1.5


def test_large_prediction_does_not_stack_on_a_code_driven_rise(fresh):
    root, mem = fresh["shop_py"]
    task = "Refactor Engine.run into smaller functions without changing behaviour"
    before = _decide(root, task, _cfg(predict=False), None, mem)
    assert any(b.startswith(("complex code", "risk high")) for b in before.bumps), before.reasons
    from ccr.route import _predict_adjust
    from ccr.evaluate.predict import parse as P
    reasons = []
    t = before.tier.index
    tier, used = _predict_adjust(t, t, P(json.dumps(LARGE)), before.task_class, before.risk,
                                 False, _cfg(), reasons, code_rose=True)
    assert tier == t and not used and "already rose" in reasons[-1]


def test_predictor_starts_the_cli_lean(monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda _: "/bin/claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cmd = L.claude_command("haiku", "low", lean=True)
    assert cmd[-3:] == ["--setting-sources", "user", "--disable-slash-commands"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert L.claude_command("haiku", "low", lean=True)[-1] == "--bare"
    assert "--bare" not in L.claude_command("haiku", "low")       # ccr explain --llm unchanged


def test_predict_timeout_fits_the_hook_timeout(tmp_path):
    from ccr.install.settings import HOOKS
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text("predict:\n  timeout_s: 30\n")
    cfg = load_config(tmp_path)
    assert any("predict.timeout_s" in e for e in cfg.errors)
    assert HOOKS["UserPromptSubmit"][1] >= 13 + 1 + 1


def _settings(root, timeout):
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "ccr hook prompt", "timeout": timeout}]}]}}))


def test_old_install_keeps_the_prediction_inside_its_hook_timeout(tmp_path):
    from ccr.hooks import _body
    from ccr.install.settings import installed_prompt_timeout
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text("hooks:\n  route_prompts: false\n")
    raw = json.dumps({"prompt": "hi", "cwd": str(tmp_path)}).encode()
    _settings(tmp_path, 5)                      # installed before 0.2.1
    assert installed_prompt_timeout(tmp_path) == 5.0
    result = {}
    _body("prompt", raw, {"CLAUDE_PROJECT_DIR": str(tmp_path)}, result)
    assert result["budget"] == 4.0              # 3 s prediction + 1 s, under Claude Code's 5 s
    _settings(tmp_path, 15)
    result = {}
    _body("prompt", raw, {"CLAUDE_PROJECT_DIR": str(tmp_path)}, result)
    assert result["budget"] == 11.0


def test_doctor_warns_about_a_short_prompt_hook_timeout(tmp_path):
    from ccr.doctor import _doctor_settings
    _settings(tmp_path, 5)
    assert any(lvl == "WARN" and "ccr init" in msg for lvl, msg in _doctor_settings(tmp_path))
    _settings(tmp_path, 15)
    assert not any("timeout" in msg for _, msg in _doctor_settings(tmp_path))


def test_no_time_left_skips_the_call(tmp_path):
    from ccr.evaluate import predict as P
    cfg = default_config()
    cfg.data["predict"]["timeout_s"] = 0.5
    calls = []
    pr, status = P.predict.__wrapped__("fix it", tmp_path, None, ["a.py"], cfg=cfg,
                                       runner=_runner(SMALL, calls))
    assert pr is None and not calls and "skipped" in status

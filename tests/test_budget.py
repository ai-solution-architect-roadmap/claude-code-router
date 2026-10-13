import json

import pytest

from ccr import history
from ccr.config import default_config
from ccr.graph import Graph
from ccr.route import decide
from ccr.route.budget import Budget, budget_pressure, state_path, transcript_tokens

NOW = 1_800_000_000.0


def write_state(root, pct=None, ts=NOW, resets_at=None, five=None):
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    week = {} if pct is None else {"used_percentage": pct}
    if resets_at is not None:
        week["resets_at"] = resets_at
    rl = {"seven_day": week}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five}
    p.write_text(json.dumps({"ts": ts, "model": {"id": "claude-sonnet"}, "rate_limits": rl}))


def cfg_with(**budget):
    c = default_config()
    c.data["budget"].update(budget)
    return c


def test_statusline_pressure_above_soft_limit(tmp_path):
    write_state(tmp_path, 85, ts=NOW - 600, five=12)
    b = budget_pressure(tmp_path, default_config(), now=NOW)
    assert b.source == "statusline" and b.used_pct == 85 and b.pressure
    assert b.five_hour_pct == 12 and b.age_s == 600
    assert "85%" in b.detail and "soft limit 80%" in b.detail


def test_statusline_below_soft_limit(tmp_path):
    write_state(tmp_path, 40)
    b = budget_pressure(tmp_path, default_config(), now=NOW)
    assert b.used_pct == 40 and not b.pressure


def test_soft_limit_is_configurable(tmp_path):
    write_state(tmp_path, 60)
    assert budget_pressure(tmp_path, cfg_with(soft_limit_pct=50), now=NOW).pressure
    assert not budget_pressure(tmp_path, cfg_with(soft_limit_pct=65), now=NOW).pressure


def test_iso_timestamp_and_boundary(tmp_path):
    write_state(tmp_path, 80, ts="2027-01-15T08:00:00Z")
    from datetime import datetime
    now = datetime.fromisoformat("2027-01-15T09:00:00+00:00").timestamp()
    b = budget_pressure(tmp_path, default_config(), now=now)
    assert b.pressure and b.age_s == 3600   # >= soft limit counts as pressure


def test_stale_state_is_ignored(tmp_path):
    write_state(tmp_path, 95, ts=NOW - 48 * 3600)
    b = budget_pressure(tmp_path, default_config(), now=NOW)
    assert b.used_pct is None and not b.pressure and b.source == "none"
    assert "stale" in b.detail


def test_window_reset_means_no_pressure(tmp_path):
    write_state(tmp_path, 97, resets_at=NOW - 60)
    b = budget_pressure(tmp_path, default_config(), now=NOW)
    assert b.used_pct == 0 and not b.pressure and "reset" in b.detail


@pytest.mark.parametrize("content", ["{not json", "[]", json.dumps({"rate_limits": "x"}),
                                     json.dumps({"rate_limits": {"seven_day": {
                                         "used_percentage": "high"}}})])
def test_malformed_state_falls_back(tmp_path, content):
    p = state_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(content)
    b = budget_pressure(tmp_path, default_config(), now=NOW)
    assert b.used_pct is None and not b.pressure


def test_missing_state_without_token_limit(tmp_path):
    b = budget_pressure(tmp_path, default_config(), now=NOW)
    assert b.used_pct is None and not b.pressure
    assert "statusline state missing" in b.detail and "weekly_token_limit" in b.detail


def test_token_fallback_from_history_window(tmp_path):
    history.append(tmp_path, {"ts": int(NOW - 3600), "class": "bugfix", "tier": "T1",
                              "tokens": 600_000})
    history.append(tmp_path, {"ts": int(NOW - 86400), "class": "feature", "tier": "T2",
                              "tokens": 300_000})
    history.append(tmp_path, {"ts": int(NOW - 10 * 86400), "class": "feature", "tier": "T2",
                              "tokens": 5_000_000})   # outside the 7-day window
    b = budget_pressure(tmp_path, cfg_with(weekly_token_limit=1_000_000), now=NOW)
    assert b.source == "tokens" and b.used_pct == pytest.approx(90.0) and b.pressure
    assert "statusline state missing" in b.detail


def test_token_fallback_adds_transcript_once(tmp_path):
    t = tmp_path / "sess-1.jsonl"
    lines = [{"type": "user", "message": {"content": "hi"}},
             {"type": "assistant", "message": {"usage": {
                 "input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 50,
                 "cache_read_input_tokens": 10_000}}},
             "garbage"]
    t.write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in lines))
    assert transcript_tokens(t) == 200
    history.append(tmp_path, {"ts": int(NOW), "session": "sess-1", "tokens": 200})  # same session
    history.append(tmp_path, {"ts": int(NOW), "session": "sess-0", "tokens": 300})
    from ccr.route.budget import token_fallback
    pct, detail = token_fallback(tmp_path, cfg_with(weekly_token_limit=1000), NOW, t)
    assert pct == pytest.approx(50.0)


def test_custom_fallback_hook_point(tmp_path):
    calls = []

    def fb(root, cfg, now):
        calls.append((root, now))
        return 91.0, "from my tracker"

    b = budget_pressure(tmp_path, default_config(), now=NOW, fallback=fb)
    assert calls and b.source == "custom" and b.used_pct == 91 and b.pressure
    assert "from my tracker" in b.detail
    write_state(tmp_path, 10)
    b = budget_pressure(tmp_path, default_config(), now=NOW, fallback=fb)
    assert b.source == "statusline" and len(calls) == 1   # statusline wins


def test_broken_fallback_never_raises(tmp_path):
    def fb(root, cfg, now):
        raise RuntimeError("boom")
    b = budget_pressure(tmp_path, default_config(), now=NOW, fallback=fb)
    assert not b.pressure and "boom" in b.detail


# ----------------------------------------------------------------------------- routing effect
PRESSURE = Budget(used_pct=90, source="statusline", pressure=True, soft_limit_pct=80,
                  detail="weekly usage 90%")


def _route(route_env, repo, task, budget):
    root, memory = route_env[repo]
    with Graph.open(root) as g:
        return decide(task, root, cfg=default_config(), graph=g, memory=memory, budget=budget,
                      history_records=[], now=NOW)


@pytest.mark.parametrize("repo,task,normal,pressured", [
    ("svc_go", "Add a /healthz endpoint to the router in internal/http/router.go", "T2", "T1"),
    ("shop_py", "Refactor Engine.run into smaller functions without changing behaviour", "T3",
     "T2"),
    ("web_ts", "Fix the bug where formatPrice shows NaN for zero amounts", "T1", "T0"),
    ("shop_py", "What does Engine.run do?", "T0", "T0"),                       # floor
    ("shop_py", "Fix the JWT signature verification vulnerability in app/auth/tokens.py",
     "T4", "T4"),                                                               # never T4
    ("web_ts", "Refactor the checkout page to use the new api client and split it into "
               "smaller components", "T3", "T3"),                               # critical path
])
def test_budget_pressure_biases_one_tier_down(route_env, repo, task, normal, pressured):
    calm = _route(route_env, repo, task, Budget(used_pct=20, source="statusline",
                                                detail="weekly usage 20%"))
    hot = _route(route_env, repo, task, PRESSURE)
    assert calm.tier.name == normal
    assert hot.tier.name == pressured
    assert int(normal[1]) - int(hot.tier.name[1]) <= 1
    text = " ".join(hot.reasons)
    if pressured != normal:
        assert "budget pressure (weekly 90% >= 80%)" in text
    elif normal == "T4":
        assert "T4 is never downgraded" in text
    elif normal != "T0":
        assert "critical path, no downgrade" in text

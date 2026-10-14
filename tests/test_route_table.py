"""Table-driven routing: sample tasks across fixture repos (Python, TypeScript, Go, Java, Rust)
mapped to the tier the default policy should pick. Token-free: no LLM, no budget pressure, no
history."""
import pytest

from ccr.cli import format_decision
from ccr.config import default_config
from ccr.graph import Graph
from ccr.route import decide

CASES = [
    # repo, task, expected tier, expected class
    ("shop_py", "What does Engine.run do?", "T0", "question"),
    ("shop_py", "Where is the pagination logic defined?", "T0", "question"),
    ("shop_py", "Fix the typo in the docstring of slugify in app/utils/strings.py", "T0",
     "trivial"),
    ("shop_py", "Rename the variable `tmp` to `result` in title_case", "T0", "trivial"),
    ("shop_py", "Fix the off-by-one error in paginate() in app/api/pagination.py; the last "
                "page is dropped", "T1", "bugfix"),
    ("shop_py", "Add a CSV export option to app/reports/export.py", "T2", "feature"),
    ("shop_py", "Refactor Engine.run into smaller functions without changing behaviour", "T3",
     "refactor"),
    ("shop_py", "Fix the bug where refunds in the payment ledger are double counted", "T4",
     "bugfix"),
    ("shop_py", "Fix the retry bug in the Stripe client when the API returns 429", "T2",
     "bugfix"),
    ("shop_py", "Add a column for phone number to users with a new migration", "T3",
     "migration"),
    ("shop_py", "Write a migration that drops the legacy email column and backfills user data "
                "in production", "T4", "migration"),
    ("shop_py", "Fix the JWT signature verification vulnerability in app/auth/tokens.py", "T4",
     "security"),
    ("shop_py", "Explain how sessions are validated in app/auth/session.py", "T0", "question"),
    ("shop_py", "Speed up the export in app/reports/export.py, it is slow for large reports",
     "T2", "perf"),
    ("shop_py", "Investigate why tests sometimes fail intermittently in the engine", "T3",
     "debugging"),
    ("shop_py", "Clean up the code", "T2", "refactor"),
    ("shop_py", "Update the README", "T0", "trivial"),
    ("shop_py", "Explain the overall architecture of the whole project and how the modules "
                "interact", "T1", "question"),
    ("shop_py", "please add a test for title_case with empty strings", "T1", "tests"),
    ("web_ts", "Change the primary button color to blue in src/components/Button.tsx", "T0",
     "trivial"),
    ("web_ts", "Add an optional icon prop to the Button component", "T2", "feature"),
    ("web_ts", "Fix the bug where formatPrice shows NaN for zero amounts", "T1", "bugfix"),
    ("web_ts", "Refactor the checkout page to use the new api client and split it into "
               "smaller components", "T3", "refactor"),
    ("web_ts", "Migrate all class components to React hooks across the codebase", "T3",
     "migration"),
    ("web_ts", "Why is the login state lost on page refresh? useAuth sometimes returns null",
     "T3", "debugging"),
    ("web_ts", "How is the API client configured?", "T0", "question"),
    ("web_ts", "Can you make a small fix to @src/components/Footer.tsx so the year is "
               "correct?", "T1", "bugfix"),
    ("svc_go", "Fix the data race in the LRU cache in internal/cache/cache.go", "T3",
     "debugging"),
    ("svc_go", "Add a /healthz endpoint to the router in internal/http/router.go", "T2",
     "feature"),
    ("svc_go", "Rotate the JWT secret handling in internal/auth/jwt.go to read from env "
               "instead of hardcoding it", "T4", "security"),
    ("svc_go", "Update the terraform config to add a new S3 bucket for backups", "T3",
     "feature"),
    ("svc_go", "Add a unit test for store.Get when the key is missing", "T1", "tests"),
    ("svc_go", "Bump the Go version in go.mod to 1.22", "T0", "trivial"),
    ("svc_go", "Find out why memory usage keeps growing in the server", "T3", "debugging"),
    ("svc_go", "something is broken, fix it", "T2", "bugfix"),
    ("svc_go", "Do a security review of the whole codebase for injection and XSS issues", "T3",
     "security"),
    ("java", "Rename Helper to StringHelper everywhere", "T1", "trivial"),
    ("java", "Explain what App.start does", "T0", "question"),
    ("rust", "Implement connection pooling with async retries in the network client", "T2",
     "feature"),
]


def _decide(route_env, repo, task, **kw):
    root, memory = route_env[repo]
    g = Graph.open(root)
    try:
        return decide(task, root, cfg=kw.pop("cfg", default_config()), graph=g, memory=memory,
                      history_records=kw.pop("history_records", []), now=0, **kw)
    finally:
        if g is not None:
            g.close()


def test_table_is_large_and_multilingual():
    assert len(CASES) >= 25
    assert {r for r, *_ in CASES} >= {"shop_py", "web_ts", "svc_go", "java", "rust"}
    assert {t for *_, t, _ in CASES} == {"T0", "T1", "T2", "T3", "T4"}


@pytest.mark.parametrize("repo,task,tier,kind", CASES, ids=[f"{c[0]}:{c[1][:40]}" for c in CASES])
def test_routing_table(route_env, repo, task, tier, kind):
    d = _decide(route_env, repo, task)
    explanation = "\n".join(format_decision(task, d))
    assert d.task_class.kind == kind, explanation
    assert d.tier.name == tier, explanation
    # explanations are never empty and always end with the final decision
    assert d.reasons and all(r.strip() for r in d.reasons)
    assert d.reasons[0].startswith("base ")
    assert d.reasons[-1].startswith(f"final {tier}: {d.tier.model}/{d.tier.effort}")
    assert "Reasoning:" in explanation and f"Tier:      {tier}" in explanation
    assert 0.0 <= d.confidence <= 1.0
    if tier == "T4":
        assert d.t4_rules, "T4 is only reachable through a critical-path rule"
    assert d.llm.startswith(("not requested", "off"))   # token-free by default


def test_escalation_never_skips_a_tier_without_t4_rule(route_env):
    for repo, task, _tier, _kind in CASES:
        d = _decide(route_env, repo, task)
        if not d.t4_rules:
            base = int(d.base[1])
            assert int(d.tier.name[1]) <= max(base + 1, 0), (task, d.reasons)
            assert d.tier.name != "T4"


def test_named_and_mentioned_files_are_counted(route_env):
    d = _decide(route_env, "web_ts", "Fix @src/components/Footer.tsx and src/lib/format.ts")
    loc = d.location
    assert loc.mentioned == ["src/components/Footer.tsx"]
    assert loc.explicit == ["src/components/Footer.tsx", "src/lib/format.ts"]
    assert loc.confidence == 1.0
    assert {c.path for c in loc.candidates[:2]} == {"src/components/Footer.tsx",
                                                    "src/lib/format.ts"}
    assert all(c.confidence == 1.0 for c in loc.candidates[:2])


def test_risk_components_are_all_reported(route_env):
    d = _decide(route_env, "shop_py", "Refactor Engine.run into smaller functions")
    comps = d.risk.components
    assert set(comps) == {"blast_radius", "complexity", "test_proximity", "churn",
                          "critical_path", "memory"}
    assert comps["blast_radius"].value > 0.3 and "dependents" in comps["blast_radius"].detail
    assert comps["complexity"].value > 0.5 and "run" in comps["complexity"].detail
    assert comps["test_proximity"].value == 0.0 and "tests/test_engine.py" in \
        comps["test_proximity"].detail
    assert comps["churn"].value > 0 and "commits" in comps["churn"].detail
    assert comps["memory"].value > 0 and "complex legacy" in comps["memory"].detail
    assert d.risk.level == "high"


def test_unresolved_path_is_reported(route_env):
    d = _decide(route_env, "shop_py", "Fix the crash in app/nope/missing.py")
    assert d.location.unresolved == ["app/nope/missing.py"]
    assert any("named but not found" in n for n in d.notes)


def test_steps_lower_a_plain_task_but_not_a_critical_one(tmp_path):
    from ccr.route import decide
    plan = ("Add a flag:\n1. add --dry-run to cli.py\n2. pass it to run()\n3. add a unit test "
            "for the flag")
    plain = decide("Add a flag to the cli, add --dry-run, pass it to run, add a unit test",
                   tmp_path, graph=None, memory=None)
    stepped = decide(plan, tmp_path, graph=None, memory=None)
    assert stepped.task_class.steps == 3
    assert stepped.tier.index <= plain.tier.index
    assert "trace" in stepped.record() and "steps" in stepped.record()["trace"]
    assert "prompt" not in str(stepped.record()["trace"]).lower()


def test_complex_code_escalates_bug_and_three_signals_escalate_two(tmp_path):
    from ccr.evaluate.risk import Component, Risk
    from ccr.route import _deep_code
    comp = lambda **kw: {k: Component(k, v) for k, v in kw.items()}  # noqa: E731
    assert _deep_code(Risk(components=comp(complexity=0.9, blast_radius=0.5,
                                           test_proximity=0.0))).startswith("complexity 0.90")
    assert _deep_code(Risk(components=comp(complexity=0.9, blast_radius=0.1,
                                           test_proximity=1.0))) is not None   # untested
    assert _deep_code(Risk(components=comp(complexity=0.9, blast_radius=0.1,
                                           test_proximity=0.0))) is None       # tested, isolated
    assert _deep_code(Risk(components=comp(complexity=0.2, blast_radius=0.9,
                                           test_proximity=1.0))) is None       # simple code


def test_three_signals_climb_two_tiers(monkeypatch, tmp_path):
    from ccr.evaluate.risk import Component, Risk
    from ccr.route import _route
    from ccr.config import default_config
    from ccr.evaluate.classify import classify
    from ccr.evaluate.locate import Location
    from ccr.route.budget import Budget
    comp = {n: Component(n, v) for n, v in
            dict(complexity=0.9, blast_radius=0.6, test_proximity=1.0).items()}
    risk = Risk(score=0.8, level="high", components=comp)
    tc = classify("fix login sometimes failing across the codebase", located=True)
    tc.kind = "bugfix"                      # base T1
    cfg = default_config()
    cfg.data["routing"]["ceiling_without_critical"] = "T4"
    d = _route(tc, Location(), risk, Budget(), [], cfg, 0.9)
    assert d.base == "T1" and d.tier.name == "T3"
    assert any(r.startswith("+2:") for r in d.reasons)
    cfg.data["routing"]["max_escalation"] = 1
    assert _route(tc, Location(), risk, Budget(), [], cfg, 0.9).tier.name == "T2"

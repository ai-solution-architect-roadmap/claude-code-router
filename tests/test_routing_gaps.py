"""Gaps found by replaying ~400 labelled prompts: plain-English migrations, production data
wipes, design requests, symptom-only bug reports, sweeping rewrites and follow-ups."""
import pytest
from test_route_table import _decide


@pytest.mark.parametrize("task,kind,tier", [
    ("delete all orders older than 2020 from the production database", None, "T4"),
    ("truncate the audit table in prod", None, "T4"),
    ("wipe the users table on prod", None, "T4"),
    ("add a nullable notes column to products", "migration", "T3"),
    ("add an index on orders.user_id", "migration", "T3"),
    ("design a plan for multi-currency support", "design", "T3"),
    ("how would you structure a plugin system for payments?", "design", "T3"),
    ("random 500s on the orders endpoint, no idea why", "debugging", "T3"),
    ("users complain about duplicate orders", "debugging", "T3"),
    ("add CSRF protection to the API", "security", "T3"),
    ("rewrite everything in rust", None, "T3"),
    ("add a comment above slugify", "trivial", "T0"),
    ("help", "question", "T0"),
    ("wheres the stripe client wired up", "question", "T0"),
])
def test_gap_cases(route_env, task, kind, tier):
    d = _decide(route_env, "shop_py", task)
    if kind:
        assert d.task_class.kind == kind, d.reasons
    assert d.tier.name == tier, d.reasons


def test_question_about_production_is_not_a_wipe(route_env):
    d = _decide(route_env, "shop_py", "what happens if we truncate the audit table in production?")
    assert d.tier.name in ("T0", "T1") and not d.t4_rules

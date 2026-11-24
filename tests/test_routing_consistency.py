"""Inconsistencies found by probing the rules with near-identical prompts: a guessed file
triggering a critical path, one version change landing on three tiers, keyword collisions
("error message", "dashboard table", "rate limit", "why ...?") and wording cancelling code risk.
Each case failed before the fix."""
import pytest
from test_route_table import _decide

from ccr.config import default_config
from ccr.evaluate.classify import classify, count_steps
from ccr.evaluate.locate import Candidate, Location
from ccr.evaluate.risk import Component, Risk, assess_risk
from ccr.route import _route
from ccr.route.budget import Budget


# ------------------------------------------------------------- 1. guessed critical files
def test_loose_word_does_not_make_a_named_file_critical(route_env):
    # "session" used to pull in app/auth/session.py as a candidate and fire the auth path
    d = _decide(route_env, "shop_py",
                "remove the unused session variable in app/utils/strings.py")
    assert d.task_class.kind == "trivial", d.reasons
    assert not d.risk.critical_files and not d.t4_rules, d.reasons
    assert d.tier.name == "T0", d.reasons


def _risk_for(candidates, explicit):
    loc = Location(candidates=candidates, explicit=explicit, confidence=1.0)
    return assess_risk(loc, "edit it", default_config())


def test_critical_path_counts_named_files_and_strong_matches_only():
    named = Candidate("app/utils/strings.py", 1.0, ["named in prompt"], indexed=False)
    weak = Candidate("app/auth/session.py", 0.5, ["path word 'session'"], indexed=False)
    strong = Candidate("app/auth/session.py", 0.85, ["symbol match"], indexed=False)
    assert _risk_for([named, weak], ["app/utils/strings.py"]).critical_files == []
    assert _risk_for([named, strong], ["app/utils/strings.py"]).critical_files == \
        ["app/auth/session.py"]
    crit = Candidate("app/auth/session.py", 1.0, ["named in prompt"], indexed=False)
    assert _risk_for([crit], ["app/auth/session.py"]).critical_files == ["app/auth/session.py"]
    # with nothing named, a located guess still counts (no change from before)
    assert _risk_for([weak], []).critical_files == ["app/auth/session.py"]


@pytest.mark.parametrize("task", ["remove the unused session variable",
                                  "delete the unused helper function", "remove unused imports"])
def test_unused_code_cleanup_is_trivial(task):
    assert classify(task).kind == "trivial"


# ------------------------------------------------------------- 2. version changes
@pytest.mark.parametrize("task", ["bump typer to 0.13", "update typer to 0.13",
                                  "upgrade typer to 0.13", "upgrade typer from 0.12 to 0.13.1"])
def test_plain_version_change_is_a_bump_whatever_the_verb(task):
    assert classify(task).kind == "trivial"


@pytest.mark.parametrize("task", ["upgrade django to 4.2", "upgrade react to 18",
                                  "upgrade postgres to 16.1", "upgrade typer to 1.0, a major release"])
def test_framework_database_or_major_upgrade_stays_a_migration(task):
    assert classify(task).kind == "migration"


def test_version_change_tiers_agree(route_env):
    tiers = {_decide(route_env, "shop_py", f"{verb} requests to 2.32").tier.name
             for verb in ("bump", "update", "upgrade")}
    assert tiers == {"T0"}


# ------------------------------------------------------------- 3. "error" and debugging
@pytest.mark.parametrize("task,kind", [
    ("fix the typo in the error message in app/api/handlers.py", "trivial"),
    ("fix typo in README", "trivial"),
    ("improve error handling", "feature"),
    ("fix the error in paginate()", "bugfix"),          # a real error is still a bug
])
def test_error_after_message_or_handling_is_not_a_bug(task, kind):
    assert classify(task).kind == kind


def test_typo_in_error_message_routes_to_t0(route_env):
    d = _decide(route_env, "shop_py", "fix the typo in the error message in app/api/handlers.py")
    assert d.tier.name == "T0", d.reasons


def test_unanchored_bug_is_debugging_only_with_hard_bug_signs(route_env):
    plain = _decide(route_env, "shop_py", "the app crashes")
    assert plain.task_class.kind == "bugfix" and plain.tier.name == "T2", plain.reasons
    assert not plain.reclassed
    hard = _decide(route_env, "shop_py", "the app sometimes crashes")
    assert hard.task_class.kind == "debugging" and hard.tier.name == "T3", hard.reasons


# ------------------------------------------------------------- 4. wording vs code risk
def _risky_bugfix(task):
    comp = {n: Component(n, v) for n, v in
            dict(complexity=0.94, blast_radius=1.0, test_proximity=0.0).items()}
    risk = Risk(score=0.5, level="medium", components=comp)
    return _route(classify(task, located=True), Location(confidence=1.0), risk, Budget(), [],
                  default_config(), 0.9)


def test_small_scope_words_do_not_cancel_code_risk():
    plain = _risky_bugfix("fix the null check in config.py")
    just = _risky_bugfix("just fix the null check in config.py")
    assert plain.tier.name == just.tier.name == "T2", just.reasons
    assert any("the code's risk sets the tier" in r for r in just.reasons)


def test_explicit_steps_still_lower_a_code_risk_rise():
    stepped = _risky_bugfix("fix the null check:\n1. open config.py\n2. guard None\n"
                            "3. add a test")
    assert stepped.tier.name == "T1", stepped.reasons


# ------------------------------------------------------------- 5. keyword collisions
@pytest.mark.parametrize("task,kind", [
    ("add a new column to the dashboard table", "feature"),
    ("add a column to the report table in the CSV export", "feature"),
    ("add a column to the users table", "migration"),
    ("add a nullable notes column to products", "migration"),
    ("add a column to the dashboard table and the orders database table", "migration"),
])
def test_column_and_table_mean_a_migration_only_outside_ui_context(task, kind):
    assert classify(task).kind == kind


@pytest.mark.parametrize("task,kind", [
    ("add a rate limit to the export command", "feature"),
    ("add rate limiting to the login endpoint", "security"),
    ("rate limit the public API", "security"),
])
def test_rate_limit_is_security_only_on_auth_or_api(task, kind):
    assert classify(task).kind == kind


@pytest.mark.parametrize("task,kind", [
    ("why does the export print the header twice?", "question"),
    ("why is the report export slow?", "question"),
    ("why does the app crash on startup?", "debugging"),
    ("why do the tests fail intermittently?", "debugging"),
])
def test_why_question_is_a_question_unless_it_names_a_failure(task, kind):
    assert classify(task).kind == kind


def test_why_question_routes_to_t0(route_env):
    d = _decide(route_env, "shop_py", "why does the export print the header twice?")
    assert d.tier.name == "T0", d.reasons


@pytest.mark.parametrize("text,steps", [
    ("1. open config.py 2. add a tier 3. update TIERS 4. run tests", 4),
    ("(1) open it (2) edit it (3) test it", 3),
    ("upgrade typer to 0.13. 2. then rerun", 0),       # not a run starting at 1
    ("1. open config.py 2. add a tier", 0),             # too short
])
def test_inline_numbered_steps(text, steps):
    assert count_steps(text) == steps

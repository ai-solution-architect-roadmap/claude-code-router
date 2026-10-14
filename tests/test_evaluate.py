import json
import os
import stat
import sys

import pytest
from typer.testing import CliRunner

from ccr import history
from ccr.cli import app
from ccr.config import default_config
from ccr.evaluate import classify, locate
from ccr.evaluate import llm as L
from ccr.evaluate.risk import critical_groups_for, glob_rx, prompt_critical_terms
from ccr.evaluate.text import identifiers, path_tokens
from ccr.graph import Graph
from ccr.memory import MemoryReport, MemorySignal
from ccr.route import decide

runner = CliRunner()


# ----------------------------------------------------------------------------- classify
@pytest.mark.parametrize("task,kind", [
    ("fix the typo in the README", "trivial"),
    ("Fix the null pointer crash in the parser", "bugfix"),
    ("Fix the deadlock in the worker pool", "debugging"),
    ("Please could you add pagination to the users endpoint", "feature"),
    ("Can you add a /healthz endpoint?", "feature"),
    ("Upgrade from Django 3 to Django 5", "migration"),
    ("Why does the cache return stale data randomly?", "debugging"),
    ("Is there a helper for slugs?", "question"),
    ("Optimize the N+1 queries in the orders list", "perf"),
    ("Sanitize user input to prevent SQL injection in search", "security"),
    ("Write integration tests for the payment webhook", "tests"),
    ("Extract the validation logic into its own module", "refactor"),
])
def test_classify_kinds(task, kind):
    assert classify(task).kind == kind


def test_classify_scope_vagueness_and_markers():
    big = classify("Rename getUser to fetchUser across the codebase")
    assert big.scope == "large" and "across the codebase" in big.scope_terms
    small = classify("Just a tiny tweak to the header")
    assert small.scope == "small"
    vague = classify("make it work better", located=False)
    assert vague.vague and vague.vagueness >= 0.6
    precise = classify("Fix KeyError in `Engine.run` at app/core/engine.py:42", located=True)
    assert precise.vagueness == 0.0
    destructive = classify("Drop the orders table and purge old rows")
    assert destructive.destructive
    assert not classify("the last page is dropped by paginate").destructive
    assert classify("") .confidence < 0.3   # nothing to go on


def test_paths_are_not_read_as_keywords():
    # 'auth' inside a path must not make this a security task
    assert classify("Fix the typo in app/auth/session.py").kind == "trivial"


def test_text_helpers():
    ats, named = path_tokens("see @src/app.ts and lib/x.py, plus README.md; not and/or 1.2.3")
    assert ats == ["src/app.ts"] and named == ["lib/x.py", "README.md"]
    ids = identifiers("Refactor `parse_args` and Engine.run plus fooBar() in Button; paginate() "
                      "and run()")
    assert {"parse_args", "Engine.run", "fooBar", "Button", "paginate", "run"} <= set(ids)
    assert "Refactor" not in ids


# ----------------------------------------------------------------------------- critical globs
def test_glob_semantics():
    assert glob_rx("**/auth/**").match("app/auth/session.py")
    assert glob_rx("**/auth/**").match("auth/x.py")
    assert glob_rx("*.tf").match("deploy/terraform/main.tf")
    assert not glob_rx("/infra/*.tf").match("x/infra/main.tf")
    assert glob_rx("/infra/*.tf").match("infra/main.tf")
    assert not glob_rx("src/*.py").match("src/a/b.py")
    groups = default_config()["critical_paths"]
    assert critical_groups_for("src/hooks/useAuth.ts", groups) == ["auth"]
    assert critical_groups_for("db/migrations/0001.sql", groups) == ["migrations"]
    assert critical_groups_for("src/components/Footer.tsx", groups) == []
    terms = prompt_critical_terms("rotate the signing key used for login", default_config()[
        "critical_terms"])
    assert ("auth", "login") in terms and ("secrets", "signing key") in terms
    assert prompt_critical_terms("add a sortable table component",
                                 default_config()["critical_terms"]) == []


# ----------------------------------------------------------------------------- locate
FILES = ["src/billing/invoice.py", "src/billing/tax.py", "src/search/rank.py",
         "src/search/index.py", "tests/test_rank.py", "README.md", "docs/guide.md"]


def test_locate_without_graph():
    loc = locate("Improve ranking quality in rank.py", ".", files=FILES)
    assert loc.explicit == ["src/search/rank.py"] and loc.confidence == 1.0
    assert loc.candidates[0].path == "src/search/rank.py"
    assert not loc.graph and any("code graph not built" in n for n in loc.notes)
    loc = locate("Tax rounding is wrong", ".", files=FILES)
    assert loc.candidates[0].path == "src/billing/tax.py" and "tax" in loc.terms
    loc = locate("look at @src/billing/ please", ".", files=FILES)
    assert loc.mentioned == ["src/billing/"]
    assert {c.path for c in loc.candidates} == {"src/billing/invoice.py", "src/billing/tax.py"}


def test_locate_memory_entities():
    mem = MemoryReport(signals=[MemorySignal("billing module", "critical",
                                             "the billing module is critical", "CLAUDE.md"),
                                MemorySignal("unrelated thing", "fragile", "x", "CLAUDE.md")])
    loc = locate("tweak the billing module emails", ".", memory=mem, files=FILES)
    assert loc.memory_terms == ["billing module"]
    hits = {c.path for c in loc.candidates if any("memory entity" in r for r in c.reasons)}
    assert hits == {"src/billing/invoice.py", "src/billing/tax.py"}


def test_locate_symbols_with_graph(route_env):
    root, mem = route_env["svc_go"]
    with Graph.open(root) as g:
        loc = locate("What does LRU.Set do?", root, graph=g, memory=mem)
    assert loc.candidates[0].path == "internal/cache/cache.go"
    assert "LRU.Set" in loc.symbols


# ----------------------------------------------------------------------------- history
def _hist(kind, area, tier, ok, bad):
    return ([{"class": kind, "area": area, "tier": tier, "outcome": "success"}] * ok +
            [{"class": kind, "area": area, "tier": tier, "outcome": "failure"}] * bad)


def _route(route_env, repo, task, records):
    root, memory = route_env[repo]
    with Graph.open(root) as g:
        return decide(task, root, cfg=default_config(), graph=g, memory=memory,
                      history_records=records, now=0)


PAGINATE = "Fix the off-by-one error in paginate() in app/api/pagination.py"


def test_history_failures_escalate_within_cap(route_env):
    d = _route(route_env, "shop_py", PAGINATE, _hist("bugfix", "app/api", "T1", 1, 3))
    assert d.area == "app/api" and d.tier.name == "T2"
    assert any(r.startswith("+1: history: T1 failed 3/4 for bugfix in app/api") for r in d.reasons)
    # failures at T2 cannot push past base+1
    d = _route(route_env, "shop_py", PAGINATE,
               _hist("bugfix", "app/api", "T1", 0, 4) + _hist("bugfix", "app/api", "T2", 0, 4))
    assert d.tier.name == "T2"


def test_history_success_one_tier_lower_deescalates(route_env):
    task = "Add a CSV export option to app/reports/export.py"
    d = _route(route_env, "shop_py", task, _hist("feature", "app/reports", "T1", 5, 0))
    assert d.tier.name == "T1"
    assert "T1 succeeded 5/5 for feature in app/reports" in d.history


def test_history_falls_back_to_class_wide(route_env):
    task = "Add a CSV export option to app/reports/export.py"
    d = _route(route_env, "shop_py", task, _hist("feature", "elsewhere", "T1", 4, 0))
    assert d.tier.name == "T1" and "feature (all areas)" in d.history
    d = _route(route_env, "shop_py", task, _hist("feature", "elsewhere", "T1", 1, 0))
    assert d.tier.name == "T2"   # below min_samples


def test_history_records_never_contain_prompt_text(tmp_path, route_env):
    secret = "Fix the bug where refunds in the payment ledger are double counted"
    d = _route(route_env, "shop_py", secret, [])
    rec = history.make_record(d, "success", tokens=1234, task=secret, prompt=secret)
    assert set(rec) <= set(history.FIELDS)
    assert rec["class"] == "bugfix" and rec["tier"] == "T4" and rec["area"] == "app/payments"
    history.append(tmp_path, {**rec, "prompt": secret})
    raw = history.history_path(tmp_path).read_text()
    assert "refund" not in raw and "ledger are" not in raw
    (history.history_path(tmp_path)).write_text(raw + "not json\n[1,2]\n\n")
    assert len(history.read(tmp_path)) == 1


# ----------------------------------------------------------------------------- LLM assist
class FakeRunner:
    def __init__(self, result=None, exc=None, cost=0.0012):
        self.calls, self.result, self.exc, self.cost = [], result, exc, cost

    def __call__(self, prompt, *, model, effort, timeout):
        self.calls.append({"prompt": prompt, "model": model, "effort": effort})
        if self.exc:
            raise self.exc
        return {"result": self.result, "total_cost_usd": self.cost, "is_error": False}


VAGUE = "Make the code cleaner somehow"


def test_llm_never_called_by_default(route_env):
    root, mem = route_env["shop_py"]
    fake = FakeRunner(json.dumps({"class": "feature"}))
    d = decide(VAGUE, root, graph=None, memory=mem, history_records=[], llm_runner=fake)
    assert not fake.calls and not d.llm_used
    assert d.llm.startswith("off (confidence")


def test_llm_called_only_below_threshold(route_env):
    root, mem = route_env["shop_py"]
    reply = 'Sure: {"class": "refactor", "scope": "large", "vague": true, ' \
            '"files": ["app/core/engine.py", "nope.py"], "confidence": 0.7, "reason": "broad"}'
    fake = FakeRunner(reply)
    with Graph.open(root) as g:
        d = decide(VAGUE, root, graph=g, memory=mem, history_records=[], allow_llm=True,
                   llm_runner=fake)
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == "haiku" and call["effort"] == "low"
    assert VAGUE in call["prompt"] and "directories:" in call["prompt"]
    assert d.llm_used and d.task_class.source == "llm"
    assert "scope unspecified -> large" in d.llm and "files +app/core/engine.py" in d.llm
    assert "[~$0.0012]" in d.llm
    assert any(c.path == "app/core/engine.py" and "suggested by LLM" in c.reasons
               for c in d.location.candidates)
    assert d.tier.name == "T3"   # refactor T2 + large scope

    sure = FakeRunner(json.dumps({"class": "bugfix"}))
    d = decide("Fix the off-by-one error in paginate() in app/api/pagination.py", root,
               graph=None, memory=mem, history_records=[], allow_llm=True, llm_runner=sure)
    assert not sure.calls and d.llm.startswith("not needed")


@pytest.mark.parametrize("runner_,needle", [
    (FakeRunner(exc=L.LLMUnavailable("`claude` CLI not found on PATH")), "unavailable"),
    (FakeRunner(exc=ValueError("kaput")), "failed"),
    (FakeRunner("I think it is a refactor"), "no usable classification"),
    (FakeRunner('{"class": "chore"}'), "no usable classification"),
])
def test_llm_failures_fall_back_to_rules(route_env, runner_, needle):
    root, mem = route_env["shop_py"]
    d = decide(VAGUE, root, graph=None, memory=mem, history_records=[], allow_llm=True,
               llm_runner=runner_)
    assert needle in d.llm and not d.llm_used
    assert d.tier.name == "T2" and d.task_class.source == "rules"


def test_parse_hint():
    h = L.parse_hint('```json\n{"class": "Perf", "scope": "small", "files": ["./a/b.py", 3],'
                     ' "confidence": 7}\n```')
    assert h.kind == "perf" and h.scope == "small" and h.files == ["a/b.py"]
    assert h.confidence == 1.0
    assert L.parse_hint("") is None and L.parse_hint("{bad json}") is None


def test_claude_command_uses_users_cli(monkeypatch):
    monkeypatch.setattr(L.shutil, "which", lambda name: "/opt/me/bin/claude")
    cmd = L.claude_command("haiku", "low")
    assert cmd[:6] == ["/opt/me/bin/claude", "-p", "--model", "haiku", "--effort", "low"]
    assert "--output-format" in cmd and "json" in cmd and "--tools" in cmd
    monkeypatch.setattr(L.shutil, "which", lambda name: None)
    with pytest.raises(L.LLMUnavailable):
        L.claude_command("haiku", "low")


def test_run_claude_with_shim(tmp_path, monkeypatch):
    """A fake `claude` on PATH: verifies stdin prompt, flags and env, no network."""
    shim = tmp_path / "bin" / "claude"
    shim.parent.mkdir()
    log = tmp_path / "log.json"
    shim.write_text(f"""#!{sys.executable}
import json, os, sys
prompt = sys.stdin.read()
json.dump({{"argv": sys.argv[1:], "internal": os.environ.get("CCR_INTERNAL"),
           "effort_env": os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"), "prompt": prompt}},
          open({str(log)!r}, "w"))
print(json.dumps({{"type": "result", "is_error": False, "total_cost_usd": 0.001,
                  "result": '{{"class": "feature", "scope": "small"}}'}}))
""")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(shim.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    hint, status = L.assist("add a flag", "summary", "feature")
    seen = json.loads(log.read_text())
    assert hint.kind == "feature" and hint.scope == "small" and hint.cost_usd == 0.001
    assert status == "LLM assist (haiku/low)"
    assert seen["argv"][:5] == ["-p", "--model", "haiku", "--effort", "low"]
    assert seen["internal"] == "1" and seen["effort_env"] is None
    assert "add a flag" in seen["prompt"]

    shim.write_text(f"#!{sys.executable}\nprint('oops')\n")
    hint, status = L.assist("x", "s", "g")
    assert hint is None and "non-JSON" in status


# ----------------------------------------------------------------------------- CLI
def test_explain_cli_text_and_json(route_env):
    root, _ = route_env["svc_go"]
    task = "Add a /healthz endpoint to the router in internal/http/router.go"
    r = runner.invoke(app, ["explain", task, "--path", str(root)])
    assert r.exit_code == 0, r.output
    assert "Tier:      T2  sonnet / medium  (agent ccr-implement)" in r.output
    assert "internal/http/router.go" in r.output and "Reasoning:" in r.output
    assert "blast radius" in r.output and "explain runs nothing" in r.output
    r = runner.invoke(app, ["explain", task, "--json", "--path", str(root)])
    data = json.loads(r.output)
    assert data["tier"] == "T2" and data["class"]["kind"] == "feature"
    assert set(data["risk"]["components"]) == {"blast_radius", "complexity", "test_proximity",
                                               "churn", "critical_path", "memory"}
    assert data["reasons"]


def test_explain_cli_without_graph(tmp_path):
    r = runner.invoke(app, ["explain", "What does this project do?", "--path", str(tmp_path)])
    assert r.exit_code == 0 and "T0" in r.output and "Traceback" not in r.output


def test_explain_requires_task():
    r = runner.invoke(app, ["explain"])
    assert r.exit_code != 0


def test_decide_is_fast(route_env):
    import time
    root, mem = route_env["shop_py"]
    with Graph.open(root) as g:
        decide("warm up", root, graph=g, memory=mem, history_records=[])
        t = time.perf_counter()
        for _ in range(5):
            decide("Refactor Engine.run into smaller functions", root, graph=g, memory=mem,
                   history_records=[])
    assert (time.perf_counter() - t) / 5 < 0.25


# ----------------------------------------------------------------------------- "session" term
@pytest.mark.parametrize("text,hit", [
    ("Summarise what we changed in this session", None),
    ("Earlier in the session you renamed the helper; also add a docstring", None),
    ("Start a new session and fix the off-by-one in paginate()", None),
    ("Rotate the session token on login", "session token"),
    ("Prevent session fixation in the login flow", "session fixation"),
    ("Guard against session hijacking", "session hijack"),
    ("Set the Secure flag on session cookies", "session cookies"),
    ("Rotate this session token after a password change", "session token"),
])
def test_session_critical_term_needs_a_security_sense(text, hit):
    from ccr.config import default_config
    from ccr.evaluate.risk import prompt_critical_terms
    terms = [t for g, t in prompt_critical_terms(text, default_config()["critical_terms"])
             if g == "auth" and t.startswith("session")]
    assert terms == ([hit] if hit else [])


def test_in_this_session_does_not_put_a_task_on_the_auth_path(route_env):
    root, memory = route_env["shop_py"]
    with Graph.open(root) as g:
        d = decide("In this session, fix the off-by-one error in paginate() in "
                   "app/api/pagination.py", root, cfg=default_config(), graph=g,
                   memory=memory, history_records=[], now=0)
    assert d.risk.critical_terms == [] and "auth" not in d.risk.critical_groups
    assert d.tier.name == "T1"


def test_length_is_not_a_signal():
    assert classify("make it work").vagueness == classify("make it work " + "x" * 5).vagueness
    short = classify("Fix KeyError in `Engine.run`", located=True)
    long = classify("Fix KeyError in `Engine.run`, which people in the team have been seeing "
                    "since last week when they run it with the new config", located=True)
    assert short.vagueness == long.vagueness == 0.0


def test_explicit_steps_are_detected():
    numbered = classify("Add a flag:\n1. add --dry-run to cli.py\n2. pass it to run()\n"
                        "3. add a test")
    assert numbered.steps == 3
    chain = classify("First add the flag, then pass it to run, finally add a test")
    assert chain.steps == 3
    assert classify("Add a dry-run flag, then test it").steps == 0   # too few to be a plan
    assert classify("Add a dry-run flag").steps == 0


@pytest.mark.parametrize("task", [
    "fix login sometimes failing", "the sync job fails intermittently",
    "flaky upload test, fails randomly"])
def test_intermittent_wording_is_a_hard_problem(task):
    assert classify(task).hard
    assert not classify("fix the login redirect").hard


def _decide(route_env, repo, task):
    return _route(route_env, repo, task, [])


def test_project_hard_terms_count_like_intermittent():
    assert not classify("fix the sync flow").hard
    tc = classify("fix the sync flow when the cache warms", hard_terms=["cache warms"])
    assert tc.hard == ["cache warms"] and tc.kind in ("bugfix", "debugging")
    assert classify("fix the thundering herd", hard_terms=["thundering herd"]).hard


def test_login_sometimes_failing_routes_to_the_deep_tier(route_env):
    d = _decide(route_env, "shop_py", "fix login sometimes failing")
    assert d.tier.name == "T3", d.reasons


def test_dropping_a_column_in_production_is_a_critical_migration(route_env):
    d = _decide(route_env, "shop_py",
                "drop the legacy_id column from the users table in production")
    assert d.task_class.kind == "migration" and d.tier.name == "T4", d.reasons


def test_terse_unclassified_task_on_impactful_code_goes_up(route_env):
    quiet = _decide(route_env, "shop_py", "tweak app/reports/export.py")
    wide = _decide(route_env, "shop_py", "tweak app/utils/strings.py")
    assert quiet.tier.name == "T2" and not quiet.bumps
    assert wide.tier.name == "T3" and wide.bumps == ["unclassified task on impactful code"]
    # a cue word or an explicit plan means the prompt did say how hard it is
    assert not any("unclassified" in r for r in
                   _decide(route_env, "shop_py", "add a flag to app/utils/strings.py").reasons)


def test_trace_carries_the_rules_that_fired(route_env):
    d = _decide(route_env, "shop_py", "fix login sometimes failing")
    tr = d.trace()
    assert "fix" in tr["rules"] and tr["hard_terms"] == ["sometimes"]
    assert any(b.startswith("hard problem") for b in tr["bumps"])

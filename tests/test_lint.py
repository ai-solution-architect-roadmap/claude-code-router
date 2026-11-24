"""Prompt linter: hints on vague prompts, silence on clear ones, strict mode, history."""
import io
import json
import shutil

import pytest

from ccr import history as H
from ccr.config import default_config, load_config
from ccr.evaluate.lint import closest, lint
from ccr.history.stats import compute, format_text
from ccr.hooks import main as hook_main
from ccr.route import decide
from conftest import FIX, git_commit_all

# The 48-prompt routing probe set (tests/test_code_first.py::PROBE and the project probe file):
# only these two are vague enough for a hint. Everything else must stay silent.
PROBE_HINTED = {"the app crashes", "improve error handling"}
PROBE = [
    "fix typo in README", "fix the typo in the error message in ccr/cli.py",
    "add tests for decide()", "add a test", "add a --verbose flag to ccr explain",
    "bump typer to 0.13", "upgrade typer to 0.13", "update typer to 0.13",
    "why does ccr explain print the reasons twice?", "why is the graph build slow?",
    "how does the budget pressure work?", "how should we handle budget pressure?",
    "how does login work in this app?", "explain the payment flow",
    "where is the stripe webhook handled?", "add rate limiting to the login endpoint",
    "add a rate limit to the export command",
    "rename decide to route_task across the whole codebase", "rename decide to route_task",
    "just quickly refactor the auth middleware", "refactor the auth middleware",
    "small change to the payment refund logic", "change the payment refund logic",
    "don't refactor anything, just fix the null check in ccr/config.py",
    "fix the null check in ccr/config.py", "Fix The Null Check In ccr/config.py",
    "the graph build is slow, make it faster", "make the graph build faster",
    "the dashboard sometimes shows the wrong totals", "the dashboard shows the wrong totals",
    "add a comment explaining the stripe retry logic", "update the login button color",
    "change the label on the checkout page", "add a new column to the dashboard table",
    "add a column to the users table", "delete all the old log files in .ccr/local",
    "drop support for python 3.9", "remove the unused session variable in ccr/hooks/stop.py",
    "fix the flaky test in tests/test_history.py", "write a plan for adding a plugin system",
    "add a plugin system", "convert the config loader to use pydantic",
    "use pydantic for the config loader", "the app crashes",
    "it crashes when I run ccr init on windows", "improve error handling",
    "improve the error handling in ccr/cli.py",
    "1. open ccr/config.py 2. add a new tier T5 3. update TIERS 4. run tests",
]


@pytest.fixture(scope="module")
def ccr_repo(tmp_path_factory):
    """A committed copy of this repository's own ccr/ package (the probe set's target)."""
    from ccr.graph import update
    root = tmp_path_factory.mktemp("lint") / "ccr_repo"
    src = FIX.parents[1]
    shutil.copytree(src / "ccr", root / "ccr", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "tests").mkdir()
    (root / "tests" / "test_history.py").write_text("def test_x():\n    pass\n")
    (root / "README.md").write_text("# ccr\n")
    git_commit_all(root)
    update(root, workers=1, offline=True)
    return root


@pytest.fixture(scope="module")
def shop(tmp_path_factory):
    from ccr.graph import update
    root = tmp_path_factory.mktemp("lint") / "shop"
    shutil.copytree(FIX / "route" / "shop_py", root)
    git_commit_all(root)
    update(root, workers=1, offline=True)
    return root


def run_lint(text, root):
    cfg = load_config(root)
    return lint(text, decide(text, root, cfg=cfg, allow_predict=False), cfg=cfg, root=root)


def test_probe_set_hints_only_the_vague_prompts(ccr_repo):
    hinted = {p for p in PROBE if run_lint(p, ccr_repo)}
    assert hinted == PROBE_HINTED


@pytest.mark.parametrize("text", ["the app crashes", "fix the bug", "it's broken",
                                  "fix it", "something is wrong"])
def test_bug_without_anchor_asks_where(ccr_repo, text):
    found = run_lint(text, ccr_repo)
    assert found and found.code == "no_anchor"
    assert "error" in found.hint and "\n" not in found.hint


@pytest.mark.parametrize("text,word", [("improve error handling", "improve"),
                                       ("make it better", "better")])
def test_vague_goal_names_the_word(ccr_repo, text, word):
    found = run_lint(text, ccr_repo)
    assert found.code == "no_anchor" and f"'{word}'" in found.hint


def test_missing_path_suggests_the_closest_file(ccr_repo):
    found = run_lint("fix the crash in ccr/confg.py", ccr_repo)
    assert found.code == "missing_path" and found.suggest == ["ccr/config.py"]
    assert "`ccr/config.py`" in found.hint
    found = run_lint("add a test to tests/test_histroy.py", ccr_repo)
    assert found.suggest == ["tests/test_history.py"]


def test_missing_path_without_a_close_match(ccr_repo):
    found = run_lint("fix the crash in billing/zzqx.py", ccr_repo)
    assert found.code == "missing_path" and found.suggest == []


def test_existing_directory_is_not_a_missing_path(ccr_repo):
    (ccr_repo / ".ccr" / "local").mkdir(parents=True, exist_ok=True)
    assert run_lint("delete all the old log files in .ccr/local", ccr_repo) is None


def fake(kind="bugfix", vague_terms=(), words=4, candidates=(), unresolved=()):
    from types import SimpleNamespace
    from ccr.evaluate.classify import TaskClass
    from ccr.evaluate.locate import Location
    tc = TaskClass(kind=kind, confidence=0.5, vague_terms=list(vague_terms), words=words)
    return SimpleNamespace(task_class=tc, location=Location(candidates=list(candidates),
                                                            unresolved=list(unresolved)))


def test_weak_location_is_offered_as_a_guess():
    from ccr.evaluate.locate import Candidate
    d = fake(vague_terms=["doesn't work"], words=5,
             candidates=[Candidate("app/payments/ledger.py", 0.3, ["path word 'ledger'"])])
    found = lint("the ledger stuff doesn't work", d)
    assert found.suggest == ["app/payments/ledger.py"]
    assert found.hint.startswith("No file named. Did you mean `app/payments/ledger.py`?")


def test_guess_from_uncommitted_edits_says_so():
    from ccr.evaluate.locate import Candidate
    d = fake(vague_terms=["the app"], candidates=[
        Candidate("app/core/engine.py", 0.3, ["uncommitted edit (12 lines), prompt names no file"])])
    found = lint("the app crashes", d)
    assert "uncommitted edits in `app/core/engine.py`" in found.hint


@pytest.mark.parametrize("kind", ["question", "design", "trivial", "tests"])
def test_non_change_kinds_are_silent(kind):
    assert lint("x", fake(kind=kind, vague_terms=["something"])) is None


def test_guess_skips_tests_and_fixtures():
    from ccr.evaluate.lint import _guess
    from ccr.evaluate.locate import Candidate, Location
    loc = Location(candidates=[Candidate("tests/fixtures/app/x.py", 0.3),
                               Candidate("tests/test_x.py", 0.3, is_test=True),
                               Candidate("app/x.py", 0.25),
                               Candidate("app/y.py", 0.1)])
    assert _guess(loc) == ("app/x.py", False)
    edits = Location(candidates=[Candidate("app/z.py", 0.3,
                                           ["uncommitted edit (4 lines), prompt names no file"])])
    assert _guess(edits) == ("app/z.py", True)


def test_closest():
    files = ["ccr/config.py", "ccr/cli.py", "app/models/user.py"]
    assert closest("ccr/confg.py", files) == "ccr/config.py"
    assert closest("src/user.py", files) == "app/models/user.py"     # same basename
    assert closest("./ccr/cli.py", files) == "ccr/cli.py"
    assert closest("totally/else.rs", files) is None
    assert closest("x.py", []) is None


def test_off_mode_is_silent(ccr_repo):
    cfg = default_config()
    cfg.data["lint"]["mode"] = "off"
    d = decide("the app crashes", ccr_repo, cfg=cfg, allow_predict=False)
    assert lint("the app crashes", d, cfg=cfg, root=ccr_repo) is None


@pytest.mark.parametrize("value,mode,error", [("strict", "strict", False), ("off", "off", False),
                                              (False, "off", False), ("loud", "hint", True)])
def test_config_lint_mode(tmp_path, value, mode, error):
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text(f"lint:\n  mode: {json.dumps(value)}\n")
    cfg = load_config(tmp_path)
    assert cfg["lint"]["mode"] == mode
    assert bool(cfg.errors) == error


def test_default_mode_is_hint():
    assert default_config()["lint"]["mode"] == "hint"


# --------------------------------------------------------------------------- hook
@pytest.fixture
def proj(shop, tmp_path):
    root = tmp_path / "shop"
    shutil.copytree(shop, root)
    return root


def hook(root, text, sid="s1"):
    out = io.StringIO()
    payload = {"session_id": sid, "cwd": str(root), "prompt": text,
               "hook_event_name": "UserPromptSubmit",
               "transcript_path": str(root / "none.jsonl")}
    rc = hook_main(["prompt"], stdin=io.BytesIO(json.dumps(payload).encode()), stdout=out,
                   environ={}, hard_exit=False)
    assert rc == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def session(root, sid="s1"):
    return json.loads((root / ".ccr" / "local" / "sessions" / f"{sid}.json").read_text())


VAGUE = "the app crashes all the time now"


def test_hook_shows_hint_and_still_routes(proj):
    obj = hook(proj, VAGUE)
    assert obj["hookSpecificOutput"]["additionalContext"].startswith("ccr: T")
    lines = obj["systemMessage"].split("\n")
    assert lines[0].startswith("ccr → T") and lines[1].startswith("ccr hint: ")
    assert session(proj)["pending"]["lint"] == {"code": "no_anchor", "suggested": False,
                                                "mode": "hint"}


def test_hook_clear_prompt_has_no_hint(proj):
    obj = hook(proj, "fix the refund rounding bug in app/payments/ledger.py")
    assert "ccr hint" not in obj["systemMessage"]
    assert "lint" not in session(proj)["pending"]


def test_hint_shown_even_with_show_in_ui_off(proj):
    (proj / ".ccr" / "config.yaml").write_text("hooks:\n  show_in_ui: false\n")
    obj = hook(proj, VAGUE)
    assert obj["systemMessage"].startswith("ccr hint: ") and "\n" not in obj["systemMessage"]


def test_nolint_and_off_mode_skip_the_hint(proj):
    assert "ccr hint" not in hook(proj, VAGUE + " !nolint")["systemMessage"]
    (proj / ".ccr" / "config.yaml").write_text("lint:\n  mode: off\n")
    assert "ccr hint" not in hook(proj, VAGUE, sid="s2")["systemMessage"]


def test_follow_up_is_not_linted(proj):
    hook(proj, "fix the refund rounding bug in app/payments/ledger.py")
    obj = hook(proj, "it still crashes")         # a retry
    assert obj is None or "ccr hint" not in obj.get("systemMessage", "")
    obj = hook(proj, "the app crashes")          # short follow-up in the same task
    assert obj is None or "ccr hint" not in obj.get("systemMessage", "")


def test_first_prompt_chatter_gets_hint_only(proj):
    obj = hook(proj, "the app crashes")    # too short to route (chatter), still worth a hint
    assert obj == {"systemMessage": obj["systemMessage"]}
    assert obj["systemMessage"].startswith("ccr hint: ")
    assert not (proj / ".ccr" / "local" / "sessions" / "s1.json").exists() or \
        "pending" not in session(proj)
    assert hook(proj, "thanks", sid="s2") is None


def test_strict_mode_blocks_once_then_lets_it_through(proj):
    (proj / ".ccr" / "config.yaml").write_text("lint:\n  mode: strict\n")
    obj = hook(proj, VAGUE)
    assert obj["decision"] == "block" and "send the same prompt again" in obj["reason"]
    assert "pending" not in session(proj)
    obj = hook(proj, "  " + VAGUE.replace(" ", "  "))   # sent again; spacing does not matter
    assert obj["hookSpecificOutput"]["additionalContext"].startswith("ccr: T")
    assert session(proj)["pending"]["lint"]["mode"] == "strict"
    assert hook(proj, "fix the refund rounding bug in app/payments/ledger.py",
                sid="s3").get("decision") is None


def test_strict_mode_nolint_goes_through(proj):
    (proj / ".ccr" / "config.yaml").write_text("lint:\n  mode: strict\n")
    assert hook(proj, VAGUE + " !nolint").get("decision") is None


def test_lint_kept_in_history_and_stats(proj):
    hook(proj, VAGUE)
    hook(proj, "thanks, now add a test for paginate() in app/api/pagination.py")
    recs = H.read(proj)
    assert recs and recs[0]["lint"]["code"] == "no_anchor"
    s = compute(recs, load_config(proj))
    assert s["lint"]["hinted"]["tasks"] == 1
    assert any(line.startswith("Prompt hints: hinted 1 task") for line in
               format_text(s))


def test_stats_without_hints_has_no_lint_line():
    s = compute([{"tier": "T1", "class": "bugfix", "source": "hook", "outcome": "success"}],
                default_config())
    assert s["lint"] is None
    assert not any(line.startswith("Prompt hints") for line in format_text(s))

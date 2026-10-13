"""`ccr init` / install: merge safety, idempotency, dry-run diff exactness, status line
wrapping, agent preservation, command detection and quoting."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ccr.cli import app
from ccr.detect import Profile
from ccr.install import InstallError, apply, plan_init, plan_profile
from ccr.install import agents as A
from ccr.install.command import (HookCommand, find_on_path, is_ours, our_subcommand, render,
                                 split)
from ccr.install.settings import SettingsError

CMD = HookCommand(["ccr"])
ABS = HookCommand(["/opt/py env/bin/python3", "-m", "ccr"], portable=False,
                  source="interpreter")
PROFILE = Profile(root="", suggested_model="haiku", suggested_effort="low")


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("def main():\n    return 1\n")
    return root, {"HOME": str(home), "PATH": ""}


def plan(root, environ, **kw):
    kw.setdefault("hook_command", CMD)
    kw.setdefault("profile", PROFILE)
    return plan_init(root, environ=environ, **kw)


def settings(root, name="settings.json"):
    return json.loads((root / ".claude" / name).read_text())


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def ours_count(data, event):
    return sum(1 for g in data["hooks"].get(event, []) for h in g["hooks"]
               if our_subcommand(h["command"]) == "hook")


# --------------------------------------------------------------------------- fresh + idempotent
def test_fresh_init_creates_everything_and_is_idempotent(env):
    root, environ = env
    p = plan(root, environ)
    rels = {c.rel for c in p.changes}
    assert rels == {".claude/settings.json", ".ccr/config.yaml", ".ccr/history.jsonl",
                    ".gitignore", *(f".claude/agents/{s.file}" for s in A.SPECS)}
    assert apply(p) is None            # nothing pre-existed, so no backups
    s = settings(root)
    assert s["model"] == "haiku" and s["effortLevel"] == "low"
    assert s["statusLine"] == {"type": "command", "command": "ccr statusline"}
    for ev, name in (("SessionStart", "session-start"), ("UserPromptSubmit", "prompt"),
                     ("SubagentStop", "stop"), ("Stop", "stop")):
        assert s["hooks"][ev] == [{"hooks": [{"type": "command", "command": f"ccr hook {name}",
                                              "timeout": 5}]}]
    assert (root / ".ccr" / "history.jsonl").read_text() == ""
    gi = (root / ".gitignore").read_text()
    assert ".ccr/graph.db*" in gi and ".ccr/local/" in gi and ".ccr/backups/" in gi
    before = snapshot(root)
    again = plan(root, environ)
    assert again.changes == []
    assert snapshot(root) == before


def test_reinit_with_new_command_replaces_not_duplicates(env):
    root, environ = env
    apply(plan(root, environ))
    p = plan(root, environ, hook_command=ABS)
    assert [c.rel for c in p.changes] == [".claude/settings.json"]
    assert apply(p) is not None        # settings.json backed up
    s = settings(root)
    for ev in ("SessionStart", "UserPromptSubmit", "SubagentStop", "Stop"):
        assert ours_count(s, ev) == 1
    cmd = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert cmd == "'/opt/py env/bin/python3' -m ccr hook prompt"
    assert s["statusLine"]["command"] == "'/opt/py env/bin/python3' -m ccr statusline"
    assert any(w.startswith("`ccr` is not on PATH") for w in p.warnings)
    assert len(list((root / ".ccr" / "backups").iterdir())) == 1


# --------------------------------------------------------------------------- user data survives
def test_existing_user_keys_and_hooks_survive(env):
    root, environ = env
    (root / ".claude").mkdir()
    user_prompt_hook = {"matcher": "", "hooks": [{"type": "command", "command": "lint-prompt"}]}
    mixed = {"hooks": [{"type": "command", "command": "echo mine"},
                       {"type": "command", "command": "ccr hook stop", "timeout": 9}]}
    original = {
        "permissions": {"allow": ["Bash(ls)"], "deny": ["Read(.env)"]},
        "env": {"FOO": "1"},
        "model": "opus",
        "effortLevel": "high",
        "hooks": {"UserPromptSubmit": [user_prompt_hook],
                  "Stop": [mixed],
                  "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                                "command": "guard"}]}]},
    }
    (root / ".claude" / "settings.json").write_text(json.dumps(original, indent=4))
    p = plan(root, environ)
    assert any("kept your 'opus'" in n for n in p.notes)
    apply(p)
    s = settings(root)
    assert s["permissions"] == original["permissions"] and s["env"] == {"FOO": "1"}
    assert s["model"] == "opus" and s["effortLevel"] == "high"     # not overridden
    assert s["hooks"]["PreToolUse"] == original["hooks"]["PreToolUse"]
    assert s["hooks"]["UserPromptSubmit"][0] == user_prompt_hook   # user group first, intact
    assert ours_count(s, "UserPromptSubmit") == 1
    # the stray ccr entry inside the user's mixed group is removed, the user's entry stays
    assert s["hooks"]["Stop"][0] == {"hooks": [{"type": "command", "command": "echo mine"}]}
    assert ours_count(s, "Stop") == 1
    # original indentation (4) is kept
    assert (root / ".claude" / "settings.json").read_text().startswith('{\n    "')
    assert plan(root, environ).changes == []


def test_force_and_profile_apply_override_model(env):
    root, environ = env
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text('{"model": "opus"}')
    apply(plan(root, environ, force=True))
    assert settings(root)["model"] == "haiku"
    (root / ".claude" / "settings.json").write_text(json.dumps({**settings(root),
                                                                "model": "opus"}))
    pp = plan_profile(root, PROFILE)
    assert [c.rel for c in pp.changes] == [".claude/settings.json"]
    apply(pp)
    s = settings(root)
    assert s["model"] == "haiku" and ours_count(s, "Stop") == 1


def test_invalid_settings_json_aborts_without_writing(env):
    root, environ = env
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{nope")
    before = snapshot(root)
    with pytest.raises(SettingsError):
        plan(root, environ)
    assert snapshot(root) == before


def test_apply_refuses_file_changed_after_planning(env):
    root, environ = env
    p = plan(root, environ)
    (root / ".gitignore").write_text("x\n")
    with pytest.raises(InstallError):
        apply(p)


# --------------------------------------------------------------------------- status line
def test_wraps_existing_statusline_once(env):
    root, environ = env
    (root / ".claude").mkdir()
    theirs = "~/bin/line.sh --color 'a b'"
    (root / ".claude" / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": theirs, "padding": 1,
                        "refreshInterval": 5}}))
    apply(plan(root, environ))
    sl = settings(root)["statusLine"]
    assert sl["padding"] == 1 and sl["refreshInterval"] == 5
    assert sl["command"].startswith("ccr statusline --wrap ")
    assert split(sl["command"]) == ["ccr", "statusline", "--wrap", theirs]
    assert plan(root, environ).changes == []                     # no double wrap
    apply(plan(root, environ, hook_command=ABS))                 # prefix change keeps wrap
    sl = settings(root)["statusLine"]
    assert split(sl["command"])[-2:] == ["--wrap", theirs]
    assert split(sl["command"])[0] == "/opt/py env/bin/python3"


def test_user_level_statusline_wrapped_in_local_settings(env):
    root, environ = env
    home = Path(environ["HOME"])
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "my-global-line"}}))
    apply(plan(root, environ))
    assert "statusLine" not in settings(root)
    loc = settings(root, "settings.local.json")
    assert split(loc["statusLine"]["command"]) == ["ccr", "statusline", "--wrap",
                                                   "my-global-line"]
    assert ".claude/settings.local.json" in (root / ".gitignore").read_text()
    assert plan(root, environ).changes == []


def test_no_statusline_flag_and_local_mode(env):
    root, environ = env
    apply(plan(root, environ))
    p = plan(root, environ, local=True, statusline=False)
    apply(p)
    s, loc = settings(root), settings(root, "settings.local.json")
    assert "hooks" not in s                     # moved, not duplicated
    assert ours_count(loc, "UserPromptSubmit") == 1
    assert s["statusLine"]["command"] == "ccr statusline"   # untouched with --no-statusline


# --------------------------------------------------------------------------- agents
def test_agent_files_marker_and_hash(env):
    root, environ = env
    apply(plan(root, environ))
    for spec in A.SPECS:
        text = (root / ".claude" / "agents" / spec.file).read_text()
        assert A.state(text) == "clean"
        assert A.declared_name(text) == spec.name
        assert text.startswith("---\n# ccr:generated sha256=")
    imp = (root / ".claude" / "agents" / "ccr-implement.md").read_text()
    assert "model: sonnet\neffort: medium\n" in imp and "use proactively" in imp.lower()
    exp = (root / ".claude" / "agents" / "Explore.md").read_text()
    assert "model: haiku" in exp and "tools: Read, Grep, Glob\n" in exp
    assert "Edit" not in exp.split("---")[1]
    assert A.state(imp.replace("focused", "tight")) == "edited"


def test_user_edited_agent_preserved_unless_force(env):
    root, environ = env
    apply(plan(root, environ))
    f = root / ".claude" / "agents" / "ccr-small.md"
    edited = f.read_text().replace("minimal change", "minimal change, with a changelog entry")
    f.write_text(edited)
    p = plan(root, environ)
    assert p.changes == [] and any("kept your edited .claude/agents/ccr-small.md" in n
                                   for n in p.notes)
    # tier change in config regenerates clean agents but still keeps the edited one
    (root / ".ccr" / "config.yaml").write_text("tiers:\n  T1: {model: haiku}\n  "
                                              "T2: {effort: high}\n")
    from ccr.config import load_config
    p = plan(root, environ, cfg=load_config(root))
    assert [c.rel for c in p.changes] == [".claude/agents/ccr-implement.md"]
    apply(p)
    assert f.read_text() == edited
    p = plan(root, environ, cfg=load_config(root), force=True)
    assert ".claude/agents/ccr-small.md" in [c.rel for c in p.changes]
    bdir = apply(p)
    assert "model: haiku" in f.read_text()
    assert (bdir / ".claude" / "agents" / "ccr-small.md").read_text() == edited


def test_foreign_agent_with_same_name_is_respected(env):
    root, environ = env
    adir = root / ".claude" / "agents"
    adir.mkdir(parents=True)
    (adir / "my-explorer.md").write_text("---\nname: Explore\ndescription: mine\n---\nhi\n")
    (adir / "ccr-quick.md").write_text("---\nname: ccr-quick\ndescription: hand made\n---\n")
    p = plan(root, environ)
    rels = {c.rel for c in p.changes}
    assert ".claude/agents/Explore.md" not in rels and ".claude/agents/ccr-quick.md" not in rels
    assert any("kept your own definition in .claude/agents/my-explorer.md" in n
               for n in p.notes)
    assert any("kept your hand-written .claude/agents/ccr-quick.md" in n for n in p.notes)


# --------------------------------------------------------------------------- dry run
def test_dry_run_diff_is_exact(env, tmp_path):
    root, environ = env
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(
        '{"permissions": {"allow": ["Bash(ls)"]},\n "statusLine": {"type": "command", '
        '"command": "echo hi"}}')                       # no trailing newline
    (root / ".gitignore").write_text("node_modules/")   # no trailing newline
    mirror = tmp_path / "mirror"
    shutil.copytree(root, mirror)
    diff = plan(root, environ).diff()
    apply(plan(mirror, environ))
    patch = tmp_path / "init.diff"
    patch.write_text(diff)
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=root,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    applied = {k: v for k, v in snapshot(mirror).items() if not k.startswith(".ccr/backups/")}
    assert snapshot(root) == applied
    assert any(k.startswith(".ccr/backups/") for k in snapshot(mirror))


def test_cli_dry_run_writes_nothing(env, monkeypatch):
    root, environ = env
    monkeypatch.setenv("HOME", environ["HOME"])
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    before = snapshot(root)
    r = CliRunner().invoke(app, ["init", "--dry-run", "--path", str(root),
                                 "--command", "ccr"])
    assert r.exit_code == 0, r.output
    assert "Dry run:" in r.output and "+++ b/.claude/settings.json" in r.output
    assert snapshot(root) == before and not (root / ".ccr").exists()
    r = CliRunner().invoke(app, ["init", "--no-graph", "--path", str(root),
                                 "--command", "ccr"])
    assert r.exit_code == 0, r.output
    assert settings(root)["hooks"]["Stop"][0]["hooks"][0]["command"] == "ccr hook stop"
    r = CliRunner().invoke(app, ["init", "--no-graph", "--path", str(root),
                                 "--command", "ccr"])
    assert "Nothing to change" in r.output


# --------------------------------------------------------------------------- commands
@pytest.mark.parametrize("cmd,expect", [
    ("ccr hook prompt", "hook"), ("/usr/local/bin/ccr hook stop", "hook"),
    ("'/opt/py env/bin/python3' -m ccr hook prompt", "hook"),
    ("& 'C:/Program Files/py/python.exe' -m ccr hook stop", "hook"),
    ("C:/x/ccr.exe statusline --wrap 'y'", "statusline"),
    ("ccr statusline", "statusline"), ("my-ccr hook x", None), ("ccr start", None),
    ("echo ccr", None), ("npx ccr-hook", None)])
def test_command_identification(cmd, expect):
    assert our_subcommand(cmd) == expect
    assert is_ours(cmd) == (expect is not None)


def test_quoting_roundtrip_posix_and_powershell():
    parts = ["C:/Program Files/py/python.exe", "-m", "ccr", "statusline", "--wrap",
             "pwsh -c \"Write-Host 'it''s'\""]
    for flavor in ("posix", "powershell"):
        s = render(parts, flavor)
        assert split(s, flavor) == parts
    assert render(["C:/py/python.exe", "-m", "ccr"], "powershell") == "C:/py/python.exe -m ccr"
    assert render(["C:/a b/p.exe", "x"], "powershell") == "& 'C:/a b/p.exe' x"
    assert HookCommand(["p"], "powershell").shell_field == "powershell"
    assert HookCommand(["p"], "posix").shell_field is None


def test_powershell_hook_entry_sets_shell(env):
    root, environ = env
    ps = HookCommand(["C:/a b/python.exe", "-m", "ccr"], "powershell", portable=False)
    apply(plan(root, environ, hook_command=ps))
    h = settings(root)["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert h["shell"] == "powershell"
    assert h["command"] == "& 'C:/a b/python.exe' -m ccr hook prompt"


def test_find_on_path_rejects_foreign_ccr(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "ccr").write_text("#!/usr/bin/env node\nrequire('claude-code-router')\n")
    (foreign / "ccr").chmod(0o755)
    assert find_on_path({"PATH": str(foreign)}) is None
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "ccr").write_text("#!/usr/bin/python3\nfrom ccr.entry import main\nmain()\n")
    (mine / "ccr").chmod(0o755)
    assert find_on_path({"PATH": f"{mine}:{foreign}"}) == str(mine / "ccr")
    # a foreign ccr earlier on PATH shadows ours in a shell, so bare `ccr` is unusable
    assert find_on_path({"PATH": f"{foreign}:{mine}"}) is None

import json

import pytest
from typer.testing import CliRunner

from ccr.cli import app

runner = CliRunner()


def test_doctor_runs(tmp_path):
    r = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert r.exit_code in (0, 1)
    assert "ccr doctor" in r.output
    assert "Traceback" not in r.output


def test_doctor_bad_json_and_verbose(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{nope")
    r = runner.invoke(app, ["doctor", "--verbose", "--path", str(tmp_path)])
    assert r.exit_code == 1
    assert "not valid JSON" in r.output
    assert (tmp_path / ".ccr" / "local" / "ccr.log").exists()


def test_doctor_warns_effort_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    r = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert "CLAUDE_CODE_EFFORT_LEVEL is set" in r.output


def test_doctor_hooks_wired(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "ccr hook stop"}]}]}}))
    r = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert "ccr hooks wired" in r.output


@pytest.mark.parametrize("cmd", [["init"], ["explain"], ["run"], ["stats"], ["feedback"], ["graph", "build"]])
def test_stubs(cmd):
    r = runner.invoke(app, cmd)
    assert r.exit_code == 0 and "not implemented yet" in r.output


def test_profile(tmp_path):
    (tmp_path / "a.zig").write_text("x")
    r = runner.invoke(app, ["profile", str(tmp_path)])
    assert r.exit_code == 0 and "Zig (1)" in r.output

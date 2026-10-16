"""Stored-prompt privacy: redaction, retention, `ccr purge`, and the doctor checks."""
import json
import subprocess
import time

from typer.testing import CliRunner

from ccr.cli import app
from ccr.config import load_config
from ccr.doctor import run_doctor
from ccr.history import prompts, redact


def test_redacts_common_secrets_and_keeps_normal_text():
    text = ("use AKIAABCDEFGHIJKLMNOP and token=abc123secretvalue, mail bob@example.com, "
            "db postgres://admin:hunter2@db.local/app, Bearer abcdefghijklmnopqrstuvwxyz1234, "
            "sk-live-abcdefghijklmnop1234, ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    out, kinds = redact.redact(text)
    for leaked in ("AKIAABCDEFGHIJKLMNOP", "abc123secretvalue", "bob@example.com", "hunter2",
                   "abcdefghijklmnopqrstuvwxyz1234", "sk-live-abcdefghijklmnop1234", "ghp_abc"):
        assert leaked not in out, leaked
    assert {"aws-key", "email", "url-credentials", "bearer", "github-token"} <= set(kinds)
    plain = "fix the off-by-one in paginate() in app/api/pagination.py"
    assert redact.redact(plain) == (plain, [])


def test_snapshot_redacts_by_default(tmp_path):
    from ccr.route import decide
    d = decide("fix login, password=hunter22secret", tmp_path, cfg=load_config(tmp_path))
    snap = prompts.snapshot("i1", "fix login, password=hunter22secret", d, d.tier, "f")
    assert "hunter22secret" not in snap["prompt"] and "secret-assignment" in snap["redacted"]
    raw = prompts.snapshot("i2", "password=hunter22secret", d, d.tier, "f", redact=False)
    assert "hunter22secret" in raw["prompt"]


def test_prune_drops_only_old_entries(tmp_path):
    now = time.time()
    prompts.append(tmp_path, {"id": "old", "ts": now - 40 * 86400, "prompt": "a"})
    prompts.append(tmp_path, {"id": "new", "ts": now - 86400, "prompt": "b"})
    prompts.append(tmp_path, {"id": "nots", "prompt": "c"})
    assert prompts.prune(tmp_path, 30, now) == 1
    assert set(prompts.read(tmp_path)) == {"new", "nots"}
    assert prompts.prune(tmp_path, 0, now) == 0


def test_purge_dry_run_then_delete(tmp_path):
    prompts.append(tmp_path, {"id": "x", "ts": time.time(), "prompt": "hello"})
    r = CliRunner().invoke(app, ["purge", "--prompts", "--path", str(tmp_path)])
    assert "dry run" in r.output and prompts.path(tmp_path).exists()
    r = CliRunner().invoke(app, ["purge", "--prompts", "--yes", "--path", str(tmp_path)])
    assert not prompts.path(tmp_path).exists()
    assert CliRunner().invoke(app, ["purge", "--path", str(tmp_path)]).exit_code == 2


def test_purge_redact_rewrites_existing(tmp_path):
    prompts.append(tmp_path, {"id": "x", "ts": time.time(), "prompt": "key sk-live-abcdefghijklmnop1234"})
    CliRunner().invoke(app, ["purge", "--redact", "--yes", "--path", str(tmp_path)])
    e = json.loads(prompts.path(tmp_path).read_text())
    assert "sk-live" not in e["prompt"] and e["redacted"]


def test_doctor_flags_ungitignored_prompt_log_and_missing_agents(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))   # ignore the user's global ignore
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".ccr").mkdir()
    (tmp_path / ".ccr" / "config.yaml").write_text("version: 1\nhooks:\n  store_prompts: true\n")
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    prompts.append(tmp_path, {"id": "x", "ts": time.time(), "prompt": "hi"})
    msgs = run_doctor(tmp_path)
    text = "\n".join(f"{lv} {m}" for lv, m in msgs)
    assert "FAIL .ccr/local/prompts.jsonl is not gitignored" in text
    assert "prompt logging is ON" in text and "tier agent file(s) missing" in text
    (tmp_path / ".gitignore").write_text(".ccr/local/\n")
    assert "FAIL .ccr/local/prompts.jsonl" not in "\n".join(m for _, m in run_doctor(tmp_path))

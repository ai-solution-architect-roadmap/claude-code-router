"""Memory readers + signal extraction.

Fixtures are generated at runtime (not checked in) so their CLAUDE.md files never leak into
real Claude Code sessions opened on this repository.
"""
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ccr.cli import app
from ccr.memory import MemorySignal, collect, matches
from ccr.memory import sources as S
from ccr.memory.signals import extract_terms, signals_from_kg, signals_from_text

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_managed(monkeypatch):
    monkeypatch.setattr(S, "MANAGED_DIRS", [])


def w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    proj = tmp_path / "work" / "my_proj.v2"
    (home / ".claude").mkdir(parents=True)
    proj.mkdir(parents=True)
    return proj.resolve(), home, {"HOME": str(home)}


def full_fixture(proj: Path, home: Path) -> None:
    w(proj, "CLAUDE.md", "# Project\n\nBe careful with `src/billing/ledger.py`: rounding is fragile.\n"
                         "See @docs/extra.md for more.\n```\nnever edit code.py inside fences\n```\n")
    w(proj, "docs/extra.md", "- The payments retry loop in src/payments/retry.go is critical.\n"
                             "- Also @docs/extra.md (cycle) and @docs/missing.md\n")
    w(proj, "CLAUDE.local.md", "Do not edit generated/ by hand.\n")
    w(proj, ".claude/rules/db.md", "## Migrations\nMigrations are hard to roll back; legacy schema.\n")
    w(proj, "services/auth/CLAUDE.md", "Owned by @alice for services/auth; ask @alice before "
                                        "changing session handling.\n")
    w(proj, "node_modules/x/CLAUDE.md", "fragile vendored thing in vendored.py\n")
    w(proj, ".claude/agent-memory/reviewer/MEMORY.md", "- `api/handlers.py` is brittle around "
                                                        "pagination\n")
    w(proj, ".claude/agent-memory-local/tester/notes.md", "flaky: tests/test_sync.py\n")
    w(home, ".claude/CLAUDE.md", "Global: keep answers short.\n")
    w(home, ".claude/agent-memory/planner/MEMORY.md", "nothing risky here\n")
    mem = home / ".claude" / "projects" / S.encode_project(str(proj)) / "memory"
    w(mem, "MEMORY.md", "- [Search](search.md): the ranking module is complex\n")
    w(mem, "search.md", "---\nname: search\n---\nsrc/search/rank.py has a known race condition.\n")
    w(proj, ".mcp.json", json.dumps({"mcpServers": {
        "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"],
                   "env": {"MEMORY_FILE_PATH": "${KG_DIR:-kg}/memory.jsonl"}},
        "other": {"command": "foo"}}}))
    w(proj, "kg/memory.jsonl", "\n".join([
        json.dumps({"type": "entity", "name": "src/cache/lru.py", "entityType": "module",
                    "observations": ["Eviction logic is tricky", "written in 2019"]}),
        "not json at all",
        json.dumps({"type": "entity", "name": "Bob", "entityType": "person", "observations": []}),
        json.dumps({"type": "relation", "from": "Bob", "to": "src/cache", "relationType": "owns"}),
        json.dumps({"type": "relation", "from": "infra/terraform", "to": "Ops team",
                    "relationType": "owned_by"}),
    ]))


def sig(rep, term, kind):
    return [s for s in rep.signals if s.path_or_term == term and s.kind == kind]


def test_no_sources_is_silent(env):
    proj, home, environ = env
    rep = collect(proj, home=home, environ=environ)
    assert rep.sources == [] and rep.signals == [] and rep.notes == []


def test_all_sources(env):
    proj, home, environ = env
    full_fixture(proj, home)
    rep = collect(proj, home=home, environ=environ)
    kinds = {(s.kind, s.scope) for s in rep.sources}
    assert ("claude_md", "user") in kinds and ("claude_md", "project") in kinds
    assert ("import", "project") in kinds          # @docs/extra.md followed once
    assert ("claude_local", "local") in kinds and ("rules", "project") in kinds
    assert ("claude_md", "nested") in kinds
    assert ("auto_memory", "user") in kinds
    assert ("mcp_memory", "project") in kinds
    agents = {(s.agent, s.scope) for s in rep.sources if s.kind == "agent_memory"}
    assert agents == {("reviewer", "project"), ("tester", "local"), ("planner", "user")}
    assert not any("node_modules" in s.path for s in rep.sources)
    assert sum(1 for s in rep.sources if s.path.endswith("extra.md")) == 1
    assert all(Path(s.path).is_absolute() for s in rep.sources)

    assert sig(rep, "src/billing/ledger.py", "fragile")
    assert not sig(rep, "code.py", "protected")    # inside a code fence
    assert sig(rep, "src/payments/retry.go", "critical")
    assert sig(rep, "generated/", "protected")
    assert sig(rep, "Migrations", "hard")          # heading used when no path in the line
    assert sig(rep, "services/auth", "ownership")
    assert sig(rep, "api/handlers.py", "fragile")
    assert sig(rep, "tests/test_sync.py", "fragile")
    assert sig(rep, "src/search/rank.py", "fragile")
    assert sig(rep, "src/cache/lru.py", "fragile")
    assert sig(rep, "src/cache", "ownership")
    assert sig(rep, "infra/terraform", "ownership")
    assert all(s.source == "mcp:memory" for s in sig(rep, "src/cache", "ownership"))
    assert rep.signals_for("src/billing/ledger.py")
    assert {s.kind for s in rep.signals_for("services/auth/session.py")} == {"ownership"}
    assert rep.signals_for("README.md") == []


def test_auto_memory_encodings_and_settings(env, tmp_path):
    proj, home, environ = env
    projects = home / ".claude" / "projects"
    # 1. '/'-only encoding (plan's observed form) still found
    slash = projects / str(proj).replace("/", "-") / "memory"
    w(slash, "MEMORY.md", "slash form\n")
    assert S.auto_memory_dir(S.make_context(proj, home, environ)) == slash
    # 2. full encoding wins over glob fallback when present
    full = projects / S.encode_project(str(proj)) / "memory"
    w(full, "MEMORY.md", "full form\n")
    assert S.auto_memory_dir(S.make_context(proj, home, environ)) == full
    # 3. glob fallback: some other separator scheme
    for d in (slash, full):
        (d / "MEMORY.md").unlink()
        d.rmdir()
        d.parent.rmdir()
    odd = projects / ("_" + str(proj).strip("/").replace("/", "__")) / "memory"
    w(odd, "MEMORY.md", "odd form\n")
    assert S.auto_memory_dir(S.make_context(proj, home, environ)) == odd
    # 4. autoMemoryDirectory setting overrides
    custom = tmp_path / "custom-mem"
    w(custom, "MEMORY.md", "the legacy importer is fragile\n")
    w(proj, ".claude/settings.json", json.dumps({"autoMemoryDirectory": str(custom)}))
    rep = collect(proj, home=home, environ=environ)
    auto = [s for s in rep.sources if s.kind == "auto_memory"]
    assert [Path(s.path).parent for s in auto] == [custom]
    # 5. empty auto memory directory is reported as a note, not a source
    (custom / "MEMORY.md").unlink()
    rep = collect(proj, home=home, environ=environ)
    assert not [s for s in rep.sources if s.kind == "auto_memory"]
    assert any("empty" in n for n in rep.notes)


def test_claude_config_dir(env, tmp_path):
    proj, home, environ = env
    cfg = tmp_path / "alt-config"
    w(home, ".claude/CLAUDE.md", "default dir: should be ignored when CLAUDE_CONFIG_DIR is set\n")
    w(cfg, "CLAUDE.md", "alt dir user memory\n")
    w(cfg / "projects" / S.encode_project(str(proj)) / "memory", "MEMORY.md", "alt auto\n")
    w(cfg, ".claude.json", json.dumps({"projects": {str(proj): {"mcpServers": {
        "kg": {"command": "x", "env": {"MEMORY_FILE_PATH": str(tmp_path / "kg.jsonl")}}}}}}))
    w(tmp_path, "kg.jsonl", json.dumps({"type": "entity", "name": "core/engine",
                                        "entityType": "area",
                                        "observations": ["load-bearing, do not break"]}) + "\n")
    rep = collect(proj, home=home, environ={**environ, "CLAUDE_CONFIG_DIR": str(cfg)})
    paths = {s.path for s in rep.sources}
    assert str(cfg / "CLAUDE.md") in paths
    assert str(home / ".claude" / "CLAUDE.md") not in paths
    assert any(s.kind == "auto_memory" and s.path.startswith(str(cfg)) for s in rep.sources)
    assert sig(rep, "core/engine", "critical")


def test_mcp_variants(env, tmp_path):
    proj, home, environ = env
    w(home, ".claude.json", json.dumps({"mcpServers": {
        "memory": {"command": "npx", "args": ["@modelcontextprotocol/server-memory"]}}}))
    rep = collect(proj, home=home, environ=environ)
    assert rep.sources == [] and any("MEMORY_FILE_PATH" in n for n in rep.notes)
    # whole-file JSON format and ~ expansion
    w(home, "kg.json", json.dumps({"entities": [{"name": "db/schema.sql", "entityType": "file",
                                                 "observations": ["Never edit by hand"]}],
                                   "relations": []}))
    w(proj, ".mcp.json", json.dumps({"mcpServers": {
        "mem": {"command": "x", "env": {"MEMORY_FILE_PATH": "~/kg.json"}}}}))
    rep = collect(proj, home=home, environ=environ)
    assert [s.kind for s in rep.sources] == ["mcp_memory"]
    assert sig(rep, "db/schema.sql", "protected")
    # file configured but missing: silently skipped
    w(proj, ".mcp.json", json.dumps({"mcpServers": {
        "mem": {"command": "x", "env": {"MEMORY_FILE_PATH": "/nonexistent/kg.jsonl"}}}}))
    assert collect(proj, home=home, environ=environ).sources == []


def test_broken_inputs_never_crash(env):
    proj, home, environ = env
    (proj / "CLAUDE.md").write_bytes(b"\xff\xfe\x00 fragile `a.py`")
    w(proj, ".claude/settings.json", "{not json")
    w(proj, ".mcp.json", "[1, 2]")
    w(home, ".claude.json", "")
    (proj / ".claude" / "agent-memory").mkdir(parents=True)
    w(proj, ".claude/agent-memory/not-a-dir.md", "x")
    rep = collect(proj, home=home, environ=environ)
    assert [s.kind for s in rep.sources] == ["claude_md"]


def test_import_depth_limit(env):
    proj, home, environ = env
    w(proj, "CLAUDE.md", "@m1.md\n")
    for i in range(1, 8):
        w(proj, f"m{i}.md", f"@m{i + 1}.md\n")
    rep = collect(proj, home=home, environ=environ)
    imports = [s for s in rep.sources if s.kind == "import"]
    assert len(imports) == S.MAX_IMPORT_DEPTH


@pytest.mark.parametrize("text,expected", [
    ("Be careful with `src/a.py`.", {("src/a.py", "fragile")}),
    ("- src/legacy/ is a mess of legacy code", {("src/legacy/", "hard")}),
    ("Never touch the billing module", {("billing module", "protected")}),
    ("owner: @team-pay owns services/pay", {("services/pay", "ownership")}),
    ("The deploy.sh script is security-sensitive", {("deploy.sh", "critical")}),
    ("Run pytest before committing.", set()),
    ("Use and/or carefully; nothing else", set()),
    ("This is fragile.", set()),  # no term, no heading
])
def test_signal_table(text, expected):
    got = {(s.path_or_term, s.kind) for s in signals_from_text(text, "x")}
    assert got == expected


def test_extract_terms():
    assert extract_terms("see `pkg/mod.py` and lib/util.ts, plus http://x.io/a") == \
        ["pkg/mod.py", "lib/util.ts"]


def test_kg_owned_by_direction():
    out = signals_from_kg([], [{"from": "api/", "to": "Ann", "relationType": "maintained_by"},
                               {"from": "Ann", "to": "web/", "relationType": "maintains"}], "mcp:m")
    assert {(s.path_or_term, s.kind) for s in out} == {("api/", "ownership"), ("web/", "ownership")}


@pytest.mark.parametrize("term,path,ok", [
    ("src/billing/ledger.py", "src/billing/ledger.py", True),
    ("ledger.py", "src/billing/ledger.py", True),
    ("src/billing", "src/billing/x.py", True),
    ("generated/", "app/generated/a.py", True),
    ("billing", "src/billing/x.py", True),
    ("billing module", "src/billing/x.py", True),
    ("src/*.sql", "src/a.sql", True),
    ("billing", "src/bill.py", False),
    ("ledger.py", "src/ledger.pyc", False),
    ("auth", "src/oauthlib/x.py", False),
])
def test_matches(term, path, ok):
    assert matches(MemorySignal(term, "fragile", "t", "s"), path) is ok


def test_doctor_memory(env, monkeypatch):
    proj, home, environ = env
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    r = runner.invoke(app, ["doctor", "--path", str(proj)])
    assert "no memory sources found" in r.output
    assert "not implemented" not in r.output
    full_fixture(proj, home)
    r = runner.invoke(app, ["doctor", "--path", str(proj)])
    assert "memory: " in r.output and "MCP memory" in r.output and "agent memory (3 agent(s))" \
        in r.output
    assert "memory signals:" in r.output

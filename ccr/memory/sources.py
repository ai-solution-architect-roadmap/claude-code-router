"""Read-only discovery of Claude Code memory sources. Missing sources are silently skipped.

Sources:
  * CLAUDE.md hierarchy: managed, user (config dir + rules/), ancestors of the project,
    project (CLAUDE.md, .claude/CLAUDE.md, .claude/rules/**), CLAUDE.local.md, nested
    subdirectory CLAUDE.md files, and @imports referenced from any of them.
  * Auto memory: <config>/projects/<encoded project path>/memory/*.md (MEMORY.md first),
    or the `autoMemoryDirectory` setting.
  * Subagent memory: .claude/agent-memory/<agent>/, .claude/agent-memory-local/<agent>/,
    <config>/agent-memory/<agent>/.
  * MCP knowledge-graph memory JSONL (server-memory), located via MEMORY_FILE_PATH in
    .mcp.json or the user config (~/.claude.json, incl. per-project mcpServers).
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Mapping

from ccr.graph.files import ALL_SKIP_DIRS

MAX_BYTES = 256_000
MAX_NESTED = 200
MAX_WALK_DIRS = 20_000
MAX_IMPORT_DEPTH = 4
MAX_DIR_FILES = 50

if sys.platform == "darwin":
    MANAGED_DIRS = [Path("/Library/Application Support/ClaudeCode")]
elif os.name == "nt":
    MANAGED_DIRS = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "ClaudeCode",
                    Path(r"C:\Program Files\ClaudeCode")]
else:
    MANAGED_DIRS = [Path("/etc/claude-code")]


@dataclass
class MemoryDoc:
    kind: str    # claude_md | claude_local | rules | import | auto_memory | agent_memory
    scope: str   # managed | user | ancestor | project | local | nested
    path: Path
    text: str
    agent: str | None = None


@dataclass
class KGMemory:
    path: Path
    server: str
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)


@dataclass
class Context:
    root: Path
    home: Path
    environ: Mapping[str, str]
    _project_paths: list[str] | None = field(default=None, repr=False)

    @property
    def config_dir(self) -> Path:
        cd = self.environ.get("CLAUDE_CONFIG_DIR")
        return Path(cd).expanduser() if cd else self.home / ".claude"

    def user_json_candidates(self) -> list[Path]:
        out = []
        if self.environ.get("CLAUDE_CONFIG_DIR"):
            out.append(self.config_dir / ".claude.json")
        out.append(self.home / ".claude.json")
        return out


def make_context(root: str | Path, home: str | Path | None = None,
                 environ: Mapping[str, str] | None = None) -> Context:
    env = dict(os.environ if environ is None else environ)
    h = Path(home) if home is not None else Path(env.get("HOME") or Path.home())
    return Context(root=Path(root).resolve(), home=h, environ=env)


# --------------------------------------------------------------------------- io helpers
def read_text(p: Path, max_bytes: int = MAX_BYTES) -> str | None:
    try:
        if not p.is_file():
            return None
        with open(p, "rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


def read_json(p: Path) -> dict | None:
    """Whole-file JSON object (config files such as ~/.claude.json can be several MB)."""
    text = read_text(p, max_bytes=50_000_000)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _expand(value: str, ctx: Context, base: Path) -> Path:
    """Expand ${VAR}, ${VAR:-default}, $VAR (from ctx.environ) and ~; relative -> base."""
    s = re.sub(r"\$\{(\w+)(?::-([^}]*))?\}|\$(\w+)",
               lambda m: ctx.environ.get(m.group(1) or m.group(3),
                                         m.group(2) if m.group(2) is not None else m.group(0)),
               value)
    if s == "~" or s.startswith("~/"):
        s = str(ctx.home) + s[1:]
    p = Path(s)
    return p if p.is_absolute() else (base / p)


# --------------------------------------------------------------------------- CLAUDE.md
_IMPORT_RX = re.compile(r"(?<![\w`@])@((?:~|\.{1,2})?/?[\w.\-/]+\.[A-Za-z0-9]+)")


def _strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def _with_imports(doc: MemoryDoc, ctx: Context, seen: set[Path], depth: int = 0
                  ) -> Iterator[MemoryDoc]:
    yield doc
    if depth >= MAX_IMPORT_DEPTH:
        return
    for m in _IMPORT_RX.finditer(_strip_code(doc.text)):
        target = _expand(m.group(1), ctx, doc.path.parent)
        try:
            key = target.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        text = read_text(target)
        if text is None:
            continue
        seen.add(key)
        yield from _with_imports(MemoryDoc("import", doc.scope, target, text), ctx, seen,
                                 depth + 1)


def _doc(kind: str, scope: str, p: Path, seen: set[Path], ctx: Context) -> Iterator[MemoryDoc]:
    try:
        key = p.resolve()
    except OSError:
        return
    if key in seen:
        return
    text = read_text(p)
    if text is None:
        return
    seen.add(key)
    yield from _with_imports(MemoryDoc(kind, scope, p, text), ctx, seen)


def _rules(d: Path, scope: str, seen: set[Path], ctx: Context) -> Iterator[MemoryDoc]:
    if not d.is_dir():
        return
    try:
        files = sorted(d.rglob("*.md"))[:MAX_DIR_FILES]
    except OSError:
        return
    for p in files:
        yield from _doc("rules", scope, p, seen, ctx)


def _nested_dirs(root: Path) -> Iterator[Path]:
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        visited += 1
        if visited > MAX_WALK_DIRS:
            return
        dirnames[:] = sorted(d for d in dirnames
                             if d not in ALL_SKIP_DIRS and not d.startswith("."))
        if Path(dirpath) != root and ("CLAUDE.md" in filenames or "CLAUDE.local.md" in filenames):
            yield Path(dirpath)


def claude_md_docs(ctx: Context, nested: bool = True) -> list[MemoryDoc]:
    seen: set[Path] = set()
    out: list[MemoryDoc] = []
    for d in MANAGED_DIRS:
        out += _doc("claude_md", "managed", d / "CLAUDE.md", seen, ctx)
    out += _doc("claude_md", "user", ctx.config_dir / "CLAUDE.md", seen, ctx)
    out += _rules(ctx.config_dir / "rules", "user", seen, ctx)
    root = ctx.root
    for anc in reversed(root.parents):
        if anc == anc.parent:  # filesystem root
            continue
        out += _doc("claude_md", "ancestor", anc / "CLAUDE.md", seen, ctx)
        out += _doc("claude_local", "ancestor", anc / "CLAUDE.local.md", seen, ctx)
    out += _doc("claude_md", "project", root / "CLAUDE.md", seen, ctx)
    out += _doc("claude_md", "project", root / ".claude" / "CLAUDE.md", seen, ctx)
    out += _rules(root / ".claude" / "rules", "project", seen, ctx)
    out += _doc("claude_local", "local", root / "CLAUDE.local.md", seen, ctx)
    if nested:
        count = 0
        for d in _nested_dirs(root):
            for name, kind in (("CLAUDE.md", "claude_md"), ("CLAUDE.local.md", "claude_local")):
                docs = list(_doc(kind, "nested", d / name, seen, ctx))
                out += docs
                count += len(docs)
            if count >= MAX_NESTED:
                break
    return out


# --------------------------------------------------------------------------- settings
def _settings_files(ctx: Context) -> list[Path]:
    """Highest precedence first: managed > project local > project > user."""
    return ([d / "managed-settings.json" for d in MANAGED_DIRS]
            + [ctx.root / ".claude" / "settings.local.json",
               ctx.root / ".claude" / "settings.json",
               ctx.config_dir / "settings.json"])


def setting(ctx: Context, key: str):
    for p in _settings_files(ctx):
        data = read_json(p)
        if data and key in data:
            return data[key], p
    return None, None


# --------------------------------------------------------------------------- auto memory
def encode_project(path: str) -> str:
    """Observed Claude Code encoding: every non-alphanumeric char becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _project_paths(ctx: Context) -> list[str]:
    """Spellings of the project path (resolved root, git top level, $PWD). Memoized on the
    context: each git call costs a subprocess."""
    if ctx._project_paths is None:
        ctx._project_paths = _compute_project_paths(ctx)
    return ctx._project_paths


def _compute_project_paths(ctx: Context) -> list[str]:
    cands = [str(ctx.root)]
    try:
        from ccr.graph import git as ggit
        gi = ggit.info(ctx.root)
        if gi and gi.top:
            cands.append(str(Path(gi.top).resolve()))
    except Exception:
        pass
    raw = ctx.environ.get("PWD")
    if raw and Path(raw).resolve() == ctx.root:
        cands.append(raw)  # unresolved spelling, e.g. /tmp vs /private/tmp
    return list(dict.fromkeys(cands))


def auto_memory_dir(ctx: Context) -> Path | None:
    value, _src = setting(ctx, "autoMemoryDirectory")
    if isinstance(value, str) and value.strip():
        base = _expand(value.strip(), ctx, ctx.root)
        for cand in [base] + [base / encode_project(p) / "memory" for p in _project_paths(ctx)] \
                + [base / encode_project(p) for p in _project_paths(ctx)]:
            if cand.is_dir():
                return cand
    projects = ctx.config_dir / "projects"
    if not projects.is_dir():
        return None
    for p in _project_paths(ctx):
        for enc in (encode_project(p), p.replace("/", "-").replace("\\", "-")):
            d = projects / enc / "memory"
            if d.is_dir():
                return d
    # glob fallback: compare names with every separator/punctuation removed
    wanted = {_norm(p) for p in _project_paths(ctx)}
    try:
        for d in sorted(projects.iterdir()):
            if d.is_dir() and _norm(d.name) in wanted and (d / "memory").is_dir():
                return d / "memory"
    except OSError:
        pass
    return None


def _md_dir(d: Path) -> list[Path]:
    try:
        files = sorted(p for p in d.glob("*.md") if p.is_file())
    except OSError:
        return []
    files.sort(key=lambda p: (p.name != "MEMORY.md", p.name))
    return files[:MAX_DIR_FILES]


def auto_memory_docs(ctx: Context) -> list[MemoryDoc]:
    d = auto_memory_dir(ctx)
    if d is None:
        return []
    out = []
    for p in _md_dir(d):
        t = read_text(p)
        if t is not None:
            out.append(MemoryDoc("auto_memory", "user", p, t))
    return out


# --------------------------------------------------------------------------- agent memory
def agent_memory_docs(ctx: Context) -> list[MemoryDoc]:
    out = []
    for base, scope in ((ctx.root / ".claude" / "agent-memory", "project"),
                        (ctx.root / ".claude" / "agent-memory-local", "local"),
                        (ctx.config_dir / "agent-memory", "user")):
        if not base.is_dir():
            continue
        try:
            agents = sorted(a for a in base.iterdir() if a.is_dir())
        except OSError:
            continue
        for a in agents:
            for p in _md_dir(a):
                t = read_text(p)
                if t is not None:
                    out.append(MemoryDoc("agent_memory", scope, p, t, agent=a.name))
    return out


# --------------------------------------------------------------------------- MCP memory
def _is_memory_server(name: str, cfg: dict) -> bool:
    env = cfg.get("env") or {}
    if isinstance(env, dict) and "MEMORY_FILE_PATH" in env:
        return True
    blob = " ".join(str(x) for x in [cfg.get("command", "")] + list(cfg.get("args") or []))
    return bool(re.search(r"server-memory|mcp-memory|memory-server|knowledge-graph", blob)) or \
        name.lower() in ("memory", "knowledge-graph", "kg-memory")


def mcp_memory_servers(ctx: Context) -> list[tuple[str, dict, Path]]:
    """(server name, config, config file) for MCP servers that look like server-memory."""
    found: list[tuple[str, dict, Path]] = []
    proj = read_json(ctx.root / ".mcp.json")
    if proj and isinstance(proj.get("mcpServers"), dict):
        found += [(n, c, ctx.root / ".mcp.json") for n, c in proj["mcpServers"].items()
                  if isinstance(c, dict)]
    for uj in ctx.user_json_candidates():
        data = read_json(uj)
        if not data:
            continue
        servers = dict(data.get("mcpServers") or {})
        projects = data.get("projects") or {}
        for key in _project_paths(ctx):
            pj = projects.get(key) if isinstance(projects, dict) else None
            if isinstance(pj, dict) and isinstance(pj.get("mcpServers"), dict):
                servers.update(pj["mcpServers"])
        found += [(n, c, uj) for n, c in servers.items() if isinstance(c, dict)]
        break  # first existing user config wins
    return [(n, c, p) for n, c, p in found if _is_memory_server(n, c)]


def parse_kg(text: str) -> tuple[list[dict], list[dict]]:
    entities, relations = [], []

    def add(obj):
        if not isinstance(obj, dict):
            return
        t = obj.get("type")
        if t == "entity" or (t is None and "name" in obj and "observations" in obj):
            entities.append(obj)
        elif t == "relation" or (t is None and "from" in obj and "to" in obj):
            relations.append(obj)

    stripped = text.lstrip()
    if stripped.startswith("{") and '"entities"' in stripped[:2000]:
        try:
            whole = json.loads(text)
            for e in whole.get("entities") or []:
                add({**e, "type": "entity"} if isinstance(e, dict) else e)
            for r in whole.get("relations") or []:
                add({**r, "type": "relation"} if isinstance(r, dict) else r)
            return entities, relations
        except ValueError:
            pass
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            add(json.loads(line))
        except ValueError:
            continue
    return entities, relations


def mcp_memory(ctx: Context) -> tuple[list[KGMemory], list[str]]:
    """Returns (knowledge graphs read, notes about servers whose file could not be found)."""
    out, notes, seen = [], [], set()
    for name, cfg, cfg_path in mcp_memory_servers(ctx):
        env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
        raw = env.get("MEMORY_FILE_PATH")
        if not raw:
            notes.append(f"MCP memory server '{name}' ({cfg_path.name}) has no MEMORY_FILE_PATH; "
                         "its default file location is not discoverable")
            continue
        p = _expand(str(raw), ctx, ctx.root)
        try:
            key = p.resolve()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        text = read_text(p, max_bytes=20_000_000)
        if text is None:
            continue
        ents, rels = parse_kg(text)
        out.append(KGMemory(path=p, server=name, entities=ents, relations=rels))
    return out, notes

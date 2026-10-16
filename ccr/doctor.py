"""Environment and project health checks behind `ccr doctor`. Each check returns
(level, message) pairs; level is OK / WARN / FAIL / INFO."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def _doctor_claude(root: Path) -> list[tuple[str, str]]:
    exe = shutil.which("claude")
    if not exe:
        return [("FAIL", "`claude` not found on PATH")]
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return [("WARN", f"claude found at {exe} but --version failed: {e}")]
    ver = (r.stdout or r.stderr).strip() or "unknown version"
    return [("OK", f"claude found at {exe} ({ver})")]


def _doctor_effort_env(root: Path) -> list[tuple[str, str]]:
    if os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"):
        return [("WARN", "CLAUDE_CODE_EFFORT_LEVEL is set; it overrides subagent effort. Unset it.")]
    return [("OK", "CLAUDE_CODE_EFFORT_LEVEL not set")]


def _doctor_settings(root: Path) -> list[tuple[str, str]]:
    settings = root / ".claude" / "settings.json"
    if not settings.exists():
        return [("INFO", ".claude/settings.json not present")]
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except (ValueError, OSError) as e:
        return [("FAIL", f".claude/settings.json is not valid JSON: {e}")]
    out = [("OK", ".claude/settings.json is valid JSON")]
    hooks = data.get("hooks")
    if isinstance(hooks, dict) and hooks:
        if "ccr" in json.dumps(hooks):
            out.append(("OK", f"ccr hooks wired ({', '.join(hooks)})"))
        else:
            out.append(("WARN", "hooks present but none call ccr"))
    else:
        out.append(("INFO", "no hooks configured (run `ccr init`)"))
    return out


def _doctor_privacy(root: Path) -> list[tuple[str, str]]:
    from ccr.config import load_config
    from ccr.history import prompts
    cfg = load_config(root)
    h = cfg["hooks"]
    pp = prompts.path(root)
    out: list[tuple[str, str]] = []
    if h["store_prompts"]:
        n = len(prompts.read(root))
        keep = (f"kept {h['prompt_retention_days']} day(s)" if h["prompt_retention_days"]
                else "kept forever")
        out.append(("INFO", f"prompt logging is ON ({n} stored, {keep}, "
                            f"redaction {'on' if h['redact_prompts'] else 'OFF'}); "
                            f"`ccr purge --prompts --yes` deletes them"))
        if not h["redact_prompts"]:
            out.append(("WARN", "hooks.redact_prompts is off: pasted secrets would be stored"))
    elif pp.exists():
        out.append(("INFO", "prompt logging is off but .ccr/local/prompts.jsonl still exists "
                            "(`ccr purge --prompts --yes`)"))
    if pp.exists() and (root / ".git").exists():
        try:
            ignored = subprocess.run(["git", "check-ignore", "-q", str(pp.relative_to(root))],
                                     cwd=root, capture_output=True).returncode == 0
        except OSError:
            ignored = True
        if not ignored:
            out.append(("FAIL", ".ccr/local/prompts.jsonl is not gitignored: it holds prompt text "
                                "(add `.ccr/local/` to .gitignore, or rerun `ccr init`)"))
    return out


def _doctor_agents(root: Path) -> list[tuple[str, str]]:
    from ccr.config import load_config
    cfg = load_config(root)
    adir = root / ".claude" / "agents"
    if not adir.is_dir():
        return [("INFO", ".claude/agents not present (run `ccr init`)")]
    missing = [f"{t.agent} ({n})" for n, t in cfg.tiers.items()
               if not (adir / f"{t.agent}.md").exists()]
    if missing:
        return [("WARN", "tier agent file(s) missing: " + ", ".join(missing)
                         + " (rerun `ccr init` to create them)")]
    return [("OK", f"all {len(cfg.tiers)} tier agents present in .claude/agents")]


def _doctor_history(root: Path) -> list[tuple[str, str]]:
    from ccr import history as H
    p = H.history_path(root)
    if not p.exists():
        return [("INFO", ".ccr/history.jsonl not present (created by `ccr init`)")]
    try:
        lines = [ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except OSError as e:
        return [("WARN", f"history unreadable: {e}")]
    raw = H.read_raw(root)
    tasks = H.resolve(raw)
    fb = sum(1 for r in raw if r.get("type") == "feedback")
    bad = len(lines) - len(raw)
    dup = len(raw) - fb - len(tasks)
    msg = (f"history: {len(tasks)} task(s), {fb} feedback record(s)"
           + (f", {dup} duplicate line(s) ignored" if dup > 0 else "")
           + (f", {bad} unreadable line(s) skipped" if bad else ""))
    out = [("OK" if not bad else "INFO", msg)]
    ga = root / ".gitattributes"
    try:
        has_union = "history.jsonl merge=union" in ga.read_text(encoding="utf-8")
    except OSError:
        has_union = False
    if (root / ".git").exists() and not has_union:
        out.append(("INFO", "no `.ccr/history.jsonl merge=union` in .gitattributes: parallel "
                            "branches may conflict on history (rerun `ccr init`)"))
    return out


def _doctor_config(root: Path) -> list[tuple[str, str]]:
    from ccr.config import config_path, load_config
    cfg = load_config(root)
    if cfg.path is None:
        return [("INFO", f"{config_path(root).relative_to(root)} not present (built-in tier "
                         f"defaults)")]
    out = [("WARN", f"config: {e}") for e in cfg.errors]
    out += [("INFO", f"config: {w}") for w in cfg.warnings]
    if not cfg.errors:
        t = cfg.tiers
        out.insert(0, ("OK", "config valid; tiers " + ", ".join(
            f"{k} {v.model}/{v.effort}" for k, v in t.items())))
    return out


def _doctor_graph(root: Path) -> list[tuple[str, str]]:
    from ccr.graph import freshness
    try:
        f = freshness(root)
    except Exception as e:  # never let doctor crash on a broken db
        return [("WARN", f"graph check failed: {e}")]
    if f["status"] == "missing":
        return [("INFO", "graph not built yet (run `ccr graph`)")]
    if f["status"] == "error":
        return [("WARN", "graph database unreadable (run `ccr graph --rebuild`)")]
    age = f.get("age_s")
    age_s = f"{age // 60} min ago" if age is not None and age < 7200 else (
        f"{age // 3600} h ago" if age is not None else "unknown age")
    msg = f"graph: {f['files']} files, mode {f['mode']}, built {age_s}"
    if f["status"] == "fresh":
        return [("OK", msg + ", fresh")]
    return [("WARN", msg + ", stale: " + "; ".join(f["reasons"]) + " (run `ccr graph`)")]


_MEM_LABELS = {"claude_md": "CLAUDE.md", "claude_local": "CLAUDE.local.md", "rules": "rules",
               "import": "@imports", "auto_memory": "auto memory", "agent_memory": "agent memory",
               "mcp_memory": "MCP memory"}


def _doctor_memory(root: Path) -> list[tuple[str, str]]:
    from ccr.memory import collect
    try:
        rep = collect(root)
    except Exception as e:  # collect never raises, but doctor must not either
        return [("WARN", f"memory check failed: {e}")]
    out: list[tuple[str, str]] = []
    if not rep.sources:
        out.append(("INFO", "no memory sources found (CLAUDE.md, auto memory, agent memory, "
                            "MCP memory)"))
    else:
        groups: dict[str, list] = {}
        for s in rep.sources:
            groups.setdefault(s.kind, []).append(s)
        parts = []
        for kind, items in groups.items():
            label = _MEM_LABELS.get(kind, kind)
            if kind == "agent_memory":
                parts.append(f"{label} ({len({i.agent for i in items})} agent(s))")
            elif kind == "mcp_memory":
                parts.append(f"{label} ({sum(i.entries for i in items)} entries)")
            else:
                scopes = sorted({i.scope for i in items})
                parts.append(f"{label} x{len(items)} ({', '.join(scopes)})")
        out.append(("OK", f"memory: {len(rep.sources)} source(s): " + "; ".join(parts)))
        kinds = rep.by_kind()
        out.append(("INFO", f"memory signals: {len(rep.signals)}" + (
            " (" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")" if kinds else "")))
    out += [("INFO", n) for n in rep.notes]
    return out


# Checks run in this order; add a check by appending a `(root) -> [(level, message)]` function.
CHECKS = (_doctor_claude, _doctor_effort_env, _doctor_settings, _doctor_config, _doctor_graph,
          _doctor_memory, _doctor_agents, _doctor_privacy, _doctor_history)


def run_doctor(root: Path) -> list[tuple[str, str]]:
    """Return list of (level, message); level in OK/WARN/FAIL/INFO."""
    out: list[tuple[str, str]] = []
    for check in CHECKS:
        out += check(root)
    return out

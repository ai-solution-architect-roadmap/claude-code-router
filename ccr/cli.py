"""ccr command line interface."""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Route Claude Code tasks to the cheapest sufficient model.",
                  no_args_is_help=True, add_completion=False)

log = logging.getLogger("ccr")


def _setup_logging(root: Path, verbose: bool) -> None:
    if not verbose:
        return
    d = root / ".ccr" / "local"
    d.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=d / "ccr.log", level=logging.DEBUG,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    typer.echo(f"verbose: logging to {d / 'ccr.log'}")


def _stub(name: str) -> None:
    typer.echo(f"ccr {name}: not implemented yet")


@app.command()
def init() -> None:
    """Install ccr config into the project."""
    _stub("init")


@app.command()
def profile(path: Path = typer.Argument(Path("."), help="Project root")) -> None:
    """Print the detected project profile."""
    from ccr.detect import detect
    p = detect(path)
    typer.echo(f"Root:       {p.root}")
    langs = ", ".join(f"{k} ({v})" for k, v in p.languages.items()) or "none detected"
    typer.echo(f"Languages:  {langs}")
    typer.echo(f"Frameworks: {', '.join(p.frameworks) or 'none detected'}")
    typer.echo(f"Files:      {p.file_count}")
    typer.echo(f"LOC:        {p.total_loc}")
    typer.echo(f"Suggested default: {p.suggested_model} / {p.suggested_effort}")


@app.command()
def explain(task: Optional[str] = typer.Argument(None)) -> None:
    """Explain the routing decision for a task."""
    _stub("explain")


@app.command()
def run(task: Optional[str] = typer.Argument(None)) -> None:
    """Run a task headlessly with routing."""
    _stub("run")


@app.command()
def stats() -> None:
    """Show routing statistics."""
    _stub("stats")


@app.command()
def feedback(verdict: Optional[str] = typer.Argument(None)) -> None:
    """Record feedback on the last routing decision."""
    _stub("feedback")


@app.command()
def graph(rebuild: bool = typer.Option(False, "--rebuild", help="Drop and rebuild the graph"),
          stats: bool = typer.Option(False, "--stats", help="Show graph statistics"),
          query: Optional[str] = typer.Option(None, "--query", help="Look up a symbol"),
          offline: bool = typer.Option(False, "--offline", help="Never download grammars"),
          path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Build/refresh the code graph (incremental), or inspect it."""
    from ccr.graph import Graph, update
    root = path.resolve()
    if rebuild or not (stats or query):
        r = update(root, rebuild=rebuild, offline=offline or None)
        kind = "built" if r.full else "updated"
        typer.echo(f"graph {kind} ({r.mode}): {r.scanned} files, parsed {r.parsed}, "
                   f"unchanged {r.unchanged}, added {r.added}, removed {r.removed} "
                   f"in {r.elapsed:.2f}s")
        if r.parsers:
            typer.echo("  parsers: " + ", ".join(f"{k} {v}" for k, v in r.parsers.items()))
    g = Graph.open(root)
    if g is None:
        if stats or query:
            typer.echo("graph not built yet; run `ccr graph`")
            raise typer.Exit(1)
        return
    with g:
        if stats:
            st = g.stats()
            meta = st["meta"]
            typer.echo(f"Files:    {st['files']} ({st['tests']} tests)")
            typer.echo(f"Symbols:  {st['symbols']} "
                       f"({', '.join(f'{k} {v}' for k, v in st['symbol_kinds'].items())})")
            typer.echo(f"Edges:    {', '.join(f'{k} {v}' for k, v in st['edges'].items()) or 'none'}")
            typer.echo(f"Languages: {', '.join(f'{k} {v}' for k, v in st['languages'].items())}")
            typer.echo(f"Parsers:  {', '.join(f'{k} {v}' for k, v in st['parsers'].items())}")
            typer.echo(f"Unresolved imports (external or unknown): {st['unresolved_imports']}")
            typer.echo(f"Mode:     {meta.get('mode')}  commit {(meta.get('commit') or '-')[:12]}")
            typer.echo(f"Churn:    {st['churn_files']} files over {meta.get('churn_days', '-')} days")
            hot = g.hot_files(5)
            if hot:
                typer.echo("Hot files: " + ", ".join(f"{h['path']} ({h['commits']})" for h in hot))
            typer.echo(f"DB size:  {st['db_bytes'] // 1024} KiB")
        if query:
            found = g.lookup(query)
            if not found:
                typer.echo(f"no symbol matching {query!r}")
                raise typer.Exit(1)
            for s in found:
                typer.echo(f"{s.kind:<9} {s.qualname}  {s.path}:{s.start_line}-{s.end_line}  "
                           f"branches {s.branches}, nesting {s.nesting}, "
                           f"callers {s.callers}, callees {s.callees}")
            if len(found) == 1:
                s = found[0]
                for name, p, line in g.callers(s.id, 10):
                    typer.echo(f"  <- {name} ({p}:{line})")
                deps = g.dependents(s.path)
                tests = g.tests_for(s.path)
                ch = g.churn(s.path)
                typer.echo(f"  file dependents: {len(deps)}; tests: "
                           f"{', '.join(t.path for t in tests[:3]) or 'none found'}"
                           + (f"; churn {ch['commits']} commits / {ch['authors']} authors"
                              if ch else ""))


def _line(level: str, msg: str) -> None:
    typer.echo(f"  [{level:<4}] {msg}")


def run_doctor(root: Path) -> list[tuple[str, str]]:
    """Return list of (level, message); level in OK/WARN/FAIL/INFO."""
    out: list[tuple[str, str]] = []
    exe = shutil.which("claude")
    if not exe:
        out.append(("FAIL", "`claude` not found on PATH"))
    else:
        try:
            r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            ver = (r.stdout or r.stderr).strip() or "unknown version"
            out.append(("OK", f"claude found at {exe} ({ver})"))
        except (OSError, subprocess.SubprocessError) as e:
            out.append(("WARN", f"claude found at {exe} but --version failed: {e}"))
    if os.environ.get("CLAUDE_CODE_EFFORT_LEVEL"):
        out.append(("WARN", "CLAUDE_CODE_EFFORT_LEVEL is set; it overrides subagent effort. Unset it."))
    else:
        out.append(("OK", "CLAUDE_CODE_EFFORT_LEVEL not set"))
    settings = root / ".claude" / "settings.json"
    if not settings.exists():
        out.append(("INFO", ".claude/settings.json not present"))
    else:
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
            out.append(("OK", ".claude/settings.json is valid JSON"))
            hooks = data.get("hooks")
            if isinstance(hooks, dict) and hooks:
                text = json.dumps(hooks)
                if "ccr" in text:
                    out.append(("OK", f"ccr hooks wired ({', '.join(hooks)})"))
                else:
                    out.append(("WARN", "hooks present but none call ccr"))
            else:
                out.append(("INFO", "no hooks configured (run `ccr init`)"))
        except (ValueError, OSError) as e:
            out.append(("FAIL", f".claude/settings.json is not valid JSON: {e}"))
    out += _doctor_graph(root)
    out += _doctor_memory(root)
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


def _doctor_memory(root: Path) -> list[tuple[str, str]]:
    return [("INFO", "memory sources check not implemented yet")]


@app.command()
def doctor(verbose: bool = typer.Option(False, "--verbose", help="Log details to .ccr/local/ccr.log"),
           path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Check the environment and project setup."""
    root = path.resolve()
    _setup_logging(root, verbose)
    typer.echo(f"ccr doctor ({root})")
    results = run_doctor(root)
    for level, msg in results:
        log.debug("%s %s", level, msg)
        _line(level, msg)
    fails = sum(1 for lv, _ in results if lv == "FAIL")
    warns = sum(1 for lv, _ in results if lv == "WARN")
    typer.echo(f"{fails} problem(s), {warns} warning(s)")
    raise typer.Exit(1 if fails else 0)


if __name__ == "__main__":
    app()

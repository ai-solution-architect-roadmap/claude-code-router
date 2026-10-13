"""ccr command line interface."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Route Claude Code tasks to the cheapest sufficient model.",
                  no_args_is_help=True, add_completion=False)
graph_app = typer.Typer(help="Code graph commands.", no_args_is_help=True)
app.add_typer(graph_app, name="graph")

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


@graph_app.command("build")
def graph_build() -> None:
    """Build or refresh the code graph."""
    _stub("graph build")


@graph_app.callback()
def _graph_cb() -> None:
    pass


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
    db = root / ".ccr" / "graph.db"
    out.append(("OK" if db.exists() else "INFO",
                "graph database present (freshness check not implemented yet)" if db.exists()
                else "graph not built yet (placeholder)"))
    out.append(("INFO", "memory sources check not implemented yet"))
    return out


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

"""ccr command line interface."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import typer

from ccr.doctor import _doctor_history, run_doctor  # noqa: F401 - re-exported
from ccr.report import format_decision

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


def _print_profile(p) -> None:
    typer.echo(f"Root:       {p.root}")
    langs = ", ".join(f"{k} ({v})" for k, v in p.languages.items()) or "none detected"
    typer.echo(f"Languages:  {langs}")
    typer.echo(f"Frameworks: {', '.join(p.frameworks) or 'none detected'}")
    typer.echo(f"Files:      {p.file_count}")
    typer.echo(f"LOC:        {p.total_loc}")
    typer.echo(f"Suggested default: {p.suggested_model} / {p.suggested_effort}")


def _show_plan(plan, dry_run: bool) -> None:
    for n in plan.notes:
        typer.echo(f"  - {n}")
    for w in plan.warnings:
        typer.echo(f"  ! {w}")
    if not plan.changes:
        typer.echo("Nothing to change (already up to date).")
        return
    if dry_run:
        typer.echo(f"Dry run: {len(plan.changes)} file(s) would change; nothing written.\n")
        typer.echo(plan.diff(), nl=False)
    else:
        for c in plan.changes:
            typer.echo(f"  {c.action:<6} {c.rel}")


@app.command()
def init(dry_run: bool = typer.Option(False, "--dry-run", help="Print the exact diff; "
                                                                "write nothing"),
         force: bool = typer.Option(False, "--force", help="Override your model/effortLevel "
                                    "and regenerate edited ccr agent files (with backups)"),
         local: bool = typer.Option(False, "--local", help="Write settings to "
                                    ".claude/settings.local.json (personal, uncommitted)"),
         no_statusline: bool = typer.Option(False, "--no-statusline",
                                            help="Do not install the status line relay"),
         no_graph: bool = typer.Option(False, "--no-graph", help="Skip building the graph"),
         command: Optional[str] = typer.Option(None, "--command", help="Command prefix hooks "
                                               "use to run ccr (default: auto-detect)"),
         path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Install ccr into the project: settings (model, hooks, status line), agents, config.

    Re-running is safe: existing user keys, hooks and edited agents are kept, and every
    modified file is backed up to .ccr/backups/ first."""
    from ccr.config import load_config
    from ccr.detect import detect
    from ccr.install import InstallError, apply, plan_init
    from ccr.install.settings import SettingsError
    root = path.resolve()
    prof = detect(root)
    typer.echo(f"ccr init ({root})")
    langs = ", ".join(f"{k} {v}" for k, v in list(prof.languages.items())[:4]) or "no code"
    typer.echo(f"  profile: {langs}; {prof.file_count} files, {prof.total_loc} LOC -> "
               f"default {prof.suggested_model}/{prof.suggested_effort}")
    if not dry_run and not no_graph:
        from ccr.graph import update
        try:
            r = update(root)
            typer.echo(f"  graph: {r.scanned} files ({'built' if r.full else 'updated'}, "
                       f"parsed {r.parsed}) in {r.elapsed:.1f}s")
        except Exception as e:  # graph is optional; routing degrades without it
            typer.echo(f"  ! graph build failed ({e}); routing works without it")
    elif dry_run and not no_graph:
        typer.echo("  graph: would be built/updated (.ccr/graph.db, git-ignored)")
    try:
        plan = plan_init(root, cfg=load_config(root), profile=prof, force=force, local=local,
                         statusline=not no_statusline, command=command)
    except (SettingsError, InstallError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    _show_plan(plan, dry_run)
    if dry_run or not plan.changes:
        return
    try:
        bdir = apply(plan)
    except (InstallError, OSError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    if bdir:
        typer.echo(f"  backups in {bdir.relative_to(root)}")
    typer.echo("Done. Start `claude` here; `ccr explain \"<task>\"` previews routing.")


@app.command()
def profile(path: Path = typer.Argument(Path("."), help="Project root"),
            apply_: bool = typer.Option(False, "--apply", help="Write the suggested model/"
                                        "effortLevel into .claude/settings.json (overrides)"),
            dry_run: bool = typer.Option(False, "--dry-run", help="With --apply: print the "
                                         "diff only"),
            local: bool = typer.Option(False, "--local", help="With --apply: write "
                                       ".claude/settings.local.json instead")) -> None:
    """Print the detected project profile; --apply sets it as the session default."""
    from ccr.detect import detect
    p = detect(path)
    _print_profile(p)
    if not apply_:
        return
    from ccr.install import apply, plan_profile
    from ccr.install.settings import SettingsError
    try:
        plan = plan_profile(path, p, local=local)
    except SettingsError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    _show_plan(plan, dry_run)
    if plan.changes and not dry_run:
        apply(plan)


@app.command()
def explain(task: str = typer.Argument(..., help="The task prompt to evaluate"),
            llm: bool = typer.Option(False, "--llm", help="Ask haiku (your claude CLI) when "
                                                         "rule confidence is low"),
            as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
            path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Show the tier decision, scores, touched files and reasoning. Runs nothing."""
    from ccr.route import decide
    d = decide(task, path.resolve(), allow_llm=llm)
    if as_json:
        typer.echo(json.dumps(d.to_dict(), indent=2))
        return
    for line in format_decision(task, d):
        typer.echo(line)


@app.command()
def run(task: Optional[str] = typer.Argument(None, help="The task; `-` or omitted reads "
                                             "stdin"),
        tier: Optional[str] = typer.Option(None, "--tier", help="Start at this tier "
                                           "(T0..T4) instead of the routed one"),
        max_turns: Optional[int] = typer.Option(None, "--max-turns", min=1,
                                                help="Turn cap per attempt (default "
                                                     "run.max_turns)"),
        max_budget_usd: Optional[float] = typer.Option(
            None, "--max-budget-usd", min=0.001, help="Spend cap for the whole run, all "
            "attempts (claude's own client-side estimate)"),
        max_escalations: Optional[int] = typer.Option(None, "--max-escalations", min=0,
                                                      max=4, help="Default "
                                                      "run.max_escalations (1)"),
        test_command: Optional[str] = typer.Option(None, "--test-command",
                                                   help="Shell command that must pass "
                                                        "(default run.test_command)"),
        no_test: bool = typer.Option(False, "--no-test", help="Skip the test command"),
        permission_mode: Optional[str] = typer.Option(None, "--permission-mode",
                                                      help="Passed to claude (e.g. "
                                                           "acceptEdits)"),
        no_llm: bool = typer.Option(False, "--no-llm", help="Rules only: never ask haiku "
                                                           "to help classify"),
        dry_run: bool = typer.Option(False, "--dry-run", help="Show the routing and the "
                                                             "command; run nothing"),
        as_json: bool = typer.Option(False, "--json", help="Print a JSON summary instead "
                                                          "of the result text"),
        path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Run a task headlessly with routing and one-tier escalation.

    Routes the task, runs your `claude -p` at that tier's model/effort, escalates one tier
    on failure and logs every attempt to .ccr/history.jsonl (no prompt text)."""
    import sys

    from ccr.config import TIERS, load_config
    from ccr.headless import build_argv, find_claude, run_task
    from ccr.route import decide
    root = path.resolve()
    if task is None or task == "-":
        task = sys.stdin.read()
    if not task or not task.strip():
        typer.echo("error: empty task", err=True)
        raise typer.Exit(2)
    if tier is not None:
        tier = tier.upper()
        if tier not in TIERS:
            typer.echo(f"error: --tier must be one of {', '.join(TIERS)}", err=True)
            raise typer.Exit(2)
    cfg = load_config(root)

    def say(msg: str) -> None:
        typer.echo(f"ccr: {msg}", err=True)

    d = decide(task, root, cfg=cfg, allow_llm=not no_llm)
    t = cfg.tier(tier) if tier else d.tier
    say(f"routed {d.tier.name} ({d.tier.model}/{d.tier.effort}) for {d.task_class.kind} in "
        f"{d.area}, confidence {d.confidence:.2f}" + (f"; starting at {t.name} (--tier)"
                                                       if tier else ""))
    say(f"learning: {d.history}")
    if d.llm_used:
        say(f"LLM: {d.llm}")
    if dry_run:
        exe = find_claude(dict(os.environ)) or "claude"
        rc = cfg["run"]
        argv = build_argv(exe, t, max_turns=max_turns or rc["max_turns"],
                          max_budget_usd=max_budget_usd if max_budget_usd is not None
                          else rc["max_budget_usd"],
                          permission_mode=permission_mode or rc["permission_mode"])
        typer.echo("would run: " + " ".join(argv) + "  < task on stdin")
        tc = test_command or rc["test_command"]
        esc = rc["max_escalations"] if max_escalations is None else max_escalations
        typer.echo(f"test command: {tc if tc and not no_test else 'none'}; "
                   f"max escalations: {esc}")
        return
    res = run_task(task, root, cfg=cfg, tier=tier, max_turns=max_turns,
                   max_budget_usd=max_budget_usd, max_escalations=max_escalations,
                   test_command=test_command, use_tests=not no_test,
                   permission_mode=permission_mode, decision=d, echo=say)
    last = res.attempts[-1] if res.attempts else None
    if as_json:
        typer.echo(json.dumps(res.to_dict(), indent=2))
    elif last is not None and last.result:
        typer.echo(last.result)
    tiers = " -> ".join(f"{a.tier} {a.outcome}" for a in res.attempts)
    say(f"{'success' if res.success else 'failed'}: {tiers}; {res.tokens} tokens, "
        f"${res.cost_usd:.4f}" + (f"; stopped: {res.stopped}" if res.stopped and
                                  not res.success else "")
        + (f"; resume with `claude --resume {last.session}`" if last and last.session
           else ""))
    if last is not None and last.outcome == "error":
        raise typer.Exit(2)
    raise typer.Exit(0 if res.success else 1)


@app.command()
def stats(as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
          days: Optional[float] = typer.Option(None, "--days", min=0.01,
                                               help="Only the last N days"),
          misroutes: bool = typer.Option(False, "--misroutes",
                                         help="List each over- and under-routed task"),
          path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Show usage, estimated savings, misroutes, tier distribution, success and escalation.

    Per tier, area and task class, plus what the learning loop does with the history."""
    from ccr import history as H
    from ccr.config import load_config
    from ccr.history.stats import compute, format_text
    root = path.resolve()
    cfg = load_config(root)
    s = compute(H.read(root, window_days=days), cfg)
    if as_json:
        typer.echo(json.dumps(s, indent=2))
        return
    for line in format_text(s, root, detail=misroutes):
        typer.echo(line)


@app.command()
def dashboard(days: Optional[float] = typer.Option(None, "--days", min=0.01,
                                                   help="Only the last N days"),
              out: Optional[Path] = typer.Option(None, "--out", help="Output file (default "
                                                 ".ccr/local/dashboard.html)"),
              open_: bool = typer.Option(False, "--open", help="Open it in the browser"),
              path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Write a self-contained HTML dashboard: charts, savings, misroutes and recent tasks."""
    from ccr import history as H
    from ccr.config import load_config
    from ccr.history import dashboard as dash
    root = path.resolve()
    cfg = load_config(root)
    target = (out or root / ".ccr" / "local" / "dashboard.html").resolve()
    dash.write(root, H.read(root, window_days=days), cfg, target)
    typer.echo(f"wrote {target}")
    if open_:
        import webbrowser
        webbrowser.open(target.as_uri())


@app.command("export-benchmark")
def export_benchmark(out: Path = typer.Option(Path("ccr-benchmark.json"), "--out",
                                              help="Output JSON file"),
                     path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Write a replay set from decided tasks with stored prompts (needs hooks.store_prompts).

    Replay it with: uv run python scripts/bench_routing.py --prompts FILE"""
    from ccr import history as H
    from ccr.history import calibrate, prompts
    root = path.resolve()
    items = calibrate.export_benchmark(H.read(root), prompts.read(root))
    if not items:
        typer.echo("no decided tasks with a stored prompt yet: set hooks.store_prompts: true, "
                   "use ccr, and mark outcomes with `ccr feedback good|bad`", err=True)
        raise typer.Exit(1)
    out.write_text(json.dumps(items, indent=1))
    typer.echo(f"wrote {len(items)} labelled prompt(s) to {out} (contains your prompt text)")


@app.command()
def calibrate(as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
              path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Fit failure odds on routing signals and compare with the rules (report only)."""
    from ccr import history as H
    from ccr.history import calibrate as C
    res = C.fit(H.read(path.resolve()))
    if as_json:
        typer.echo(json.dumps(res, indent=2))
        return
    for line in C.format_text(res):
        typer.echo(line)


@app.command()
def shadow(from_tier: str = typer.Option("T3", "--from-tier",
                                         help="Sample tasks routed to this tier or above"),
           lower: Optional[str] = typer.Option(None, "--shadow-tier", help="Tier to try instead "
                                               "(default: one below the task's tier)"),
           sample: int = typer.Option(3, "--sample", min=1, max=50, help="How many tasks to re-run"),
           max_budget_usd: Optional[float] = typer.Option(None, "--max-budget-usd", min=0.01,
                                                          help="Total spend cap across the sample"),
           test_command: Optional[str] = typer.Option(None, "--test-command", help="Must exit 0 "
                                                      "for a shadow run to count as success"),
           yes: bool = typer.Option(False, "--yes", help="Actually run (spends real usage)"),
           path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Re-run sampled deep-tier tasks at a lower tier to measure over-routing (opt-in, costs usage).

    Runs your own `claude -p` in a throwaway git worktree of HEAD. Prompts come from
    `hooks.store_prompts`. Without --yes it only prints the plan."""
    from ccr import history as H
    from ccr.config import TIERS, load_config
    from ccr.history import prompts, shadow as S
    root = path.resolve()
    if from_tier not in TIERS or (lower and lower not in TIERS):
        typer.echo(f"error: tiers are {', '.join(TIERS)}", err=True)
        raise typer.Exit(2)
    cfg = load_config(root)
    picks = S.pick(H.read(root), prompts.read(root), {e["id"] for e in S.read(root)},
                   from_tier, sample)
    if not picks:
        typer.echo("nothing to sample: need successful tasks at or above "
                   f"{from_tier} with stored prompts (hooks.store_prompts: true)", err=True)
        raise typer.Exit(1)
    if max_budget_usd is None:
        typer.echo("error: --max-budget-usd is required (total cap across the sample)", err=True)
        raise typer.Exit(2)
    per = round(max_budget_usd / len(picks), 4)
    tests = (f"tests: {test_command}" if test_command else
             "NO test command: success will only mean claude exited cleanly")
    typer.echo(f"plan: re-run {len(picks)} task(s) at {lower or 'one tier below'}, up to ${per} "
               f"each (worst case ${max_budget_usd}); {tests}")
    typer.echo("note: this uses your own claude login. On a subscription it draws on your plan "
               "limits; with an API key it is billed in dollars.")
    if not yes:
        typer.echo("dry run: add --yes to execute")
        raise typer.Exit(0)
    logged = prompts.read(root)
    for rec in picks:
        entry = S.run_one(root, rec, logged[rec["id"]]["prompt"],
                          S.shadow_tier(rec["tier"], lower), cfg, budget=per,
                          test_command=test_command)
        S.append(root, entry)
        typer.echo(f"  {rec['id'][:8]} {entry['deep_tier']}->{entry['shadow_tier']}: "
                   f"{entry['outcome']} ({entry['reason']})")
    for line in S.format_lines(S.summarize(S.read(root))):
        typer.echo(line)


@app.command()
def tune(apply: bool = typer.Option(False, "--apply", help="Write the changes (backs up the config)"),
         path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Propose config edits from misroute evidence as a diff. Never changes anything without --apply."""
    import shutil
    import time
    from ccr import history as H
    from ccr.config import config_path, load_config
    from ccr.history import tune as TU
    root = path.resolve()
    cfg = load_config(root)
    changes = TU.propose(H.read(root), cfg)
    if not changes:
        typer.echo("no changes proposed (not enough decided misroutes yet)")
        return
    cp = config_path(root)
    old = cp.read_text(encoding="utf-8") if cp.exists() else "version: 1\n"
    new = TU.apply_text(old, changes)
    for ch in changes:
        typer.echo(f"- {ch['why']}")
    for line in TU.diff(old, new):
        typer.echo(line)
    if not apply:
        typer.echo("\nnot applied: run `ccr tune --apply` to write this")
        return
    tmp = cp.with_suffix(".tune.tmp")
    tmp.write_text(new, encoding="utf-8")
    check = load_config(path=tmp)
    if check.errors:
        tmp.unlink(missing_ok=True)
        typer.echo("error: the edited config would be invalid: " + "; ".join(check.errors), err=True)
        raise typer.Exit(2)
    if cp.exists():
        bk = root / ".ccr" / "backups"
        bk.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cp, bk / f"config.yaml.{int(time.time())}")
    tmp.replace(cp)
    typer.echo(f"applied to {cp} (backup in .ccr/backups/)")


@app.command()
def purge(prompts_: bool = typer.Option(False, "--prompts", help="Stored prompts and decisions"),
          shadow_: bool = typer.Option(False, "--shadow", help="Shadow-run results"),
          older_than: Optional[int] = typer.Option(None, "--older-than", min=1,
                                                   help="Only entries older than N days (prompts)"),
          redact_: bool = typer.Option(False, "--redact", help="Re-scrub existing stored prompts"),
          yes: bool = typer.Option(False, "--yes", help="Actually delete (default: show only)"),
          path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Delete or scrub locally stored prompts and shadow results. Shows what it would do first."""
    import json
    from ccr.history import prompts as P, shadow as S
    root = path.resolve()
    if not (prompts_ or shadow_ or redact_):
        typer.echo("choose what to purge: --prompts, --shadow, or --redact", err=True)
        raise typer.Exit(2)
    pp, sp = P.path(root), S.path(root)
    entries = P.read(root)
    typer.echo(f"prompts:  {len(entries)} stored ({pp.stat().st_size if pp.exists() else 0} bytes)")
    typer.echo(f"shadow:   {len(S.read(root))} result(s)")
    if not yes:
        typer.echo("dry run: add --yes to apply")
        return
    if prompts_:
        if older_than:
            typer.echo(f"removed {P.prune(root, older_than)} prompt(s) older than {older_than} days")
        else:
            pp.unlink(missing_ok=True)
            typer.echo("removed all stored prompts")
    if shadow_:
        sp.unlink(missing_ok=True)
        typer.echo("removed shadow results")
    if redact_ and pp.exists():
        n = 0
        out = []
        for e in P.read(root).values():
            text, kinds = P.redact_text(str(e.get("prompt") or ""))
            n += bool(kinds)
            out.append(json.dumps({**e, "prompt": text, "redacted": sorted(
                set(e.get("redacted") or []) | set(kinds))}, separators=(",", ":"), default=str))
        pp.write_text("\n".join(out) + ("\n" if out else ""), encoding="utf-8")
        typer.echo(f"re-scrubbed {n} prompt(s) that held secrets")


@app.command()
def feedback(verdict: Optional[str] = typer.Argument(None, help="good | bad (omit to show "
                                                     "the last task)"),
             last: bool = typer.Option(True, "--last", help="Target the most recent routed "
                                       "task (the default)"),
             ref: Optional[str] = typer.Option(None, "--id", help="Target a task by id "
                                               "(prefix) instead"),
             path: Path = typer.Option(Path("."), "--path", help="Project root")) -> None:
    """Mark the last routed task good or bad.

    Works for interactive (hook) tasks, including one still in progress, and `ccr run`
    attempts. Appends a correction record; history is never rewritten."""
    from ccr.history import feedback as F
    root = path.resolve()
    if verdict is None:
        t = F.find(root, ref)
        typer.echo(F.describe(t) if t else "no routed task found")
        raise typer.Exit(0 if t else 1)
    verdict = verdict.lower()
    if verdict not in ("good", "bad"):
        typer.echo("error: verdict must be good or bad", err=True)
        raise typer.Exit(2)
    t, _ = F.mark(root, verdict, ref)
    if t is None:
        typer.echo("no matching routed task found" + (f" for id {ref!r}" if ref else ""),
                   err=True)
        raise typer.Exit(1)
    typer.echo(f"marked {verdict} ({'success' if verdict == 'good' else 'failure'}): "
               f"{F.describe(t)}")


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


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def hook(ctx: typer.Context,
         name: str = typer.Argument(..., help="session-start | prompt | stop")) -> None:
    """Claude Code hook entry (reads hook JSON on stdin; always exits 0)."""
    from ccr.hooks import main as hook_main
    raise typer.Exit(hook_main([name, *ctx.args]))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def statusline(ctx: typer.Context) -> None:
    """Status line relay: records model/effort/rate limits, prints a short line.
    `--wrap "<cmd>"` runs your own status line command and prints its output instead."""
    from ccr.statusline import main as sl_main
    raise typer.Exit(sl_main(list(ctx.args)))


if __name__ == "__main__":
    app()

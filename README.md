# ccr: the cheapest sufficient model for Claude Code

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Built for Claude Code](https://img.shields.io/badge/built%20for-Claude%20Code-d97757.svg)](https://claude.com/claude-code)
[![Rule-based](https://img.shields.io/badge/routing-rule--based%20%26%20local-success.svg)](#how-it-works)
[![Last commit](https://img.shields.io/github/last-commit/ai-solution-architect-roadmap/claude-code-router)](https://github.com/ai-solution-architect-roadmap/claude-code-router/commits/main)

`ccr` routes each Claude Code task to the cheapest model and effort level that should be
enough for it, so your subscription goes further without you picking models by hand. You keep
typing plain `claude`.

```
ccr: T2 (sonnet/medium) — touches app/payments/ledger.py, 2 dependents, no tests found,
critical path: payments. Delegate implementation to ccr-implement; use Explore (haiku) for search.
```

## How it works

For every prompt, a hook decides a tier using local, rule-based checks (no tokens spent, under
half a second):

1. **Classify** the task: question, bugfix, feature, refactor, migration, security, and so on.
2. **Locate** the code it touches, using a local tree-sitter code graph (`.ccr/graph.db`).
3. **Score risk**: dependents, complexity, test coverage, churn, critical paths (auth, payments,
   migrations, infra, secrets) and notes in `CLAUDE.md` or memory.
4. **Adjust** with your team's past outcomes and your weekly usage.
5. **Hint** Claude with one line, which delegates the work to a tiered `ccr-*` subagent.

| Tier | Model / effort  | Agent           | Typical work |
|------|-----------------|-----------------|--------------|
| T0   | haiku / low     | `ccr-quick`     | questions, lookups, typos, renames |
| T1   | sonnet / low    | `ccr-small`     | tests, clear bug fixes, small edits |
| T2   | sonnet / medium | `ccr-implement` | features, refactors, perf work |
| T2H  | sonnet / high   | `ccr-senior`    | risky or complex multi-file changes |
| T3   | opus / high     | `ccr-architect` | migrations, security, unclear bugs, design |
| T4   | opus / xhigh    | `ccr-critical`  | critical-path changes only |

## Install

Requires Python 3.10+, git, and Claude Code (`claude`) on PATH.

```sh
uv tool install git+<repo-url>      # or: pipx install git+<repo-url>
ccr --help
```

## Quick start

```sh
cd your-project
ccr init --dry-run     # show the exact diff of every file it would touch; writes nothing
ccr init               # apply it (modified files are backed up to .ccr/backups/)
ccr doctor             # check the setup
ccr explain "Fix the off-by-one in paginate() in app/api/pagination.py"   # preview a decision
claude                 # work as usual
```

Re-running `ccr init` is safe. To skip routing for one prompt, put `!noroute` at the end of it.

## Commands

Every command accepts `--path DIR` (project root, default `.`) and `--help`. Run `ccr --help`
for the list.

### Setup

**`ccr init`** installs settings (model, hooks, status line), the `ccr-*` agents and
`.ccr/config.yaml`. Safe to re-run; modified files are backed up to `.ccr/backups/`.

| Option | Effect |
|--------|--------|
| `--dry-run` | Print the exact diff; write nothing |
| `--force` | Override your `model`/`effortLevel` and regenerate edited ccr agent files (with backups) |
| `--local` | Write settings to `.claude/settings.local.json` (personal, uncommitted) |
| `--no-statusline` | Do not install the status line relay |
| `--no-graph` | Skip building the code graph |
| `--command CMD` | Command prefix the hooks use to run ccr (default: auto-detect) |

```sh
ccr init --dry-run
ccr init --local --no-statusline
```

**`ccr profile [PATH]`** prints the detected languages, frameworks, file count and LOC, and the
suggested session default model/effort.

| Option | Effect |
|--------|--------|
| `--apply` | Write the suggestion into `.claude/settings.json` (overrides existing values) |
| `--dry-run` | With `--apply`: print the diff only |
| `--local` | With `--apply`: write `.claude/settings.local.json` instead |

**`ccr doctor`** checks the environment and project setup. `--verbose` logs details to
`.ccr/local/ccr.log`.

**`ccr graph`** builds or refreshes the code graph incrementally, or inspects it.

| Option | Effect |
|--------|--------|
| `--rebuild` | Drop and rebuild the graph |
| `--stats` | Show graph statistics |
| `--query SYMBOL` | Look up a symbol |
| `--offline` | Never download tree-sitter grammars |

### Routing

**`ccr explain "<task>"`** shows the tier, scores, touched files and reasoning. Runs nothing.
`--json` gives machine-readable output; `--llm` asks haiku (your `claude` CLI) when rule
confidence is low.

```sh
ccr explain "Fix the off-by-one in paginate() in app/api/pagination.py"
```

**`ccr run "<task>"`** runs the task headlessly with `claude -p` at the routed tier and climbs
one tier if the attempt fails. Omit the task or pass `-` to read it from stdin.

| Option | Effect |
|--------|--------|
| `--tier T0..T4` | Start at this tier instead of the routed one |
| `--max-turns N` | Turn cap per attempt (default `run.max_turns`) |
| `--max-budget-usd X` | Spend cap for the whole run, all attempts |
| `--max-escalations N` | Tiers to climb after a failure, 0 to 4 (default 1) |
| `--test-command CMD` | Shell command that must exit 0 for success (default `run.test_command`) |
| `--no-test` | Skip the test command |
| `--permission-mode MODE` | Passed to claude, e.g. `acceptEdits` |
| `--no-llm` | Rules only; never ask haiku to help classify |
| `--dry-run` | Show the routing and the command; run nothing |
| `--json` | Print a JSON summary instead of the result text |

```sh
ccr run "add a --dry-run flag to app/main.py" --test-command "pytest -q" --max-budget-usd 2
```

### Feedback, stats and learning

**`ccr feedback [good|bad]`** marks the last routed task good or bad (a correction record is
appended; history is never rewritten). With no verdict it shows the last task. Use `--id PREFIX`
to target a task by id instead of the most recent one.

**`ccr stats`** shows usage, estimated savings, misroutes, tier distribution, and success and
escalation rates per tier, area and task class.

| Option | Effect |
|--------|--------|
| `--days N` | Only the last N days |
| `--misroutes` | List each over- and under-routed task |
| `--json` | Machine-readable output |

**`ccr dashboard`** writes a self-contained HTML dashboard (charts, savings, misroutes, recent
tasks). Options: `--days N`, `--out FILE` (default `.ccr/local/dashboard.html`), `--open` to open
it in the browser.

**`ccr calibrate`** fits failure odds on the routing signals and compares them with the rules.
Report only; it never changes routing. Needs about 30 decided tasks. `--json` for machine output.

**`ccr tune`** proposes config edits from misroute evidence as a diff. Nothing changes unless
you pass `--apply`, which backs up the config first.

**`ccr shadow`** re-runs sampled deep-tier tasks at a lower tier in a throwaway git worktree to
measure over-routing. Opt-in and costs real usage; without `--yes` it only prints the plan.
Needs `hooks.store_prompts`.

| Option | Effect |
|--------|--------|
| `--from-tier T` | Sample tasks routed to this tier or above (default `T3`) |
| `--shadow-tier T` | Tier to try instead (default: one below the task's tier) |
| `--sample N` | How many tasks to re-run, 1 to 50 (default 3) |
| `--max-budget-usd X` | Total spend cap across the sample |
| `--test-command CMD` | Must exit 0 for a shadow run to count as success |
| `--yes` | Actually run |

**`ccr export-benchmark`** writes a replay set from decided tasks with stored prompts to
`--out FILE` (default `ccr-benchmark.json`). Replay it with
`uv run python scripts/bench_routing.py --prompts FILE`.

### Privacy

**`ccr purge`** deletes or scrubs locally stored prompts and shadow results. It shows what it
would do first and deletes only with `--yes`.

| Option | Effect |
|--------|--------|
| `--prompts` | Stored prompts and decisions |
| `--shadow` | Shadow-run results |
| `--older-than N` | Only prompts older than N days |
| `--redact` | Re-scrub existing stored prompts |
| `--yes` | Actually delete |

### Internal (called by Claude Code, not by you)

- **`ccr hook session-start|prompt|stop`** is the hook entry. It reads hook JSON on stdin and
  always exits 0.
- **`ccr statusline`** is the status line relay. It records model, effort and rate limits, and
  prints a short line. `--wrap "<cmd>"` runs your own status line command and prints its output.

## Configuration

`ccr init` creates `.ccr/config.yaml`; commit it. Any key you leave out uses the default. You
can change the tiers, class base tiers, critical paths and terms, risk weights, budget limits
and learning thresholds. See the [full reference](docs/REFERENCE.md).

## Accuracy

`scripts/bench_routing.py` replays a labelled prompt set against a fixture project with no API
calls. It reports under-routed and over-routed prompts and the estimated cost against all-Opus
and all-Sonnet baselines. The labels are judgment calls, so edit them to match your own work.
It measures routing quality, not answer quality.

Routing is strongest for clearly worded prompts in a supported language with a fresh graph.
It is weaker for prompts that name no code and for projects with unusual folder names until
you add your own `critical_paths`, `critical_terms` and `hard_terms`.

## Privacy

All routing decisions run locally. The only model call ccr makes on its own is an optional,
opt-in classification by haiku through your own `claude` CLI. Local state lives in `.ccr/`.

## Documentation

The [reference](docs/REFERENCE.md) covers the full configuration, team workflow, status line and
budget pressure, hooks and fail-open behavior, headless runs, feedback and learning, privacy,
limitations and troubleshooting.

## Development

```sh
uv sync
uv run pytest
uv run python scripts/bench_routing.py
```

## License

[MIT](LICENSE)

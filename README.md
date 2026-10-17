# ccr: the cheapest sufficient model for Claude Code

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

| Command | What it does |
|---------|--------------|
| `ccr init` | Install settings, hooks, agents and config into the project |
| `ccr explain "<task>"` | Show which tier a task gets and why; runs nothing |
| `ccr run "<task>"` | Headless run at the routed tier; climbs a tier if the attempt fails |
| `ccr doctor` | Validate the setup and config |
| `ccr stats` | Success and escalation rates by tier, and estimated savings |
| `ccr feedback good\|bad` | Record the outcome of the last task |
| `ccr calibrate` | Check how well the routing signals predict failures |
| `ccr shadow` | Compare against a cheaper tier on sampled tasks (spends real usage) |

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

# ccr: cheapest sufficient model for Claude Code

`ccr` routes each Claude Code task to the cheapest model and effort that should be enough
for it, so a subscription goes further without you picking models by hand. You keep
typing plain `claude`. ccr sets the session defaults, installs tiered subagents, and adds
hooks that give Claude a one-line routing hint for every prompt, based on:

- the task's class (question, bugfix, migration and so on);
- the code it touches, from a local code graph;
- risk: blast radius, complexity, tests, churn, critical paths and memory notes;
- your team's past outcomes;
- your weekly usage.

```
ccr: T2 (sonnet/medium) — touches app/payments/ledger.py, 2 dependents, no tests found,
critical path: payments. Delegate implementation to ccr-implement; use Explore (haiku) for search.
```

Everything that decides a tier is rule-based, token-free and local. The only model call ccr
makes on its own is an optional, opt-in classification by haiku through your own `claude`
CLI.

---

## Install

Requires Python 3.10+, git, and Claude Code (`claude`) on PATH.

```sh
# with uv (recommended)
uv tool install git+<repo-url>                                # or: uv tool install /path/to/checkout
# or with pipx
pipx install git+<repo-url>                                   # or: pipx install /path/to/checkout

ccr --help
```

Install ccr so `ccr` is on PATH (not just inside a virtualenv). `ccr init` writes the hook
commands as a bare `ccr`, which works on every teammate's machine. If ccr is not on PATH,
init falls back to an absolute `<python> -m ccr` and warns you. Use `--local` to keep that
machine-specific path out of the shared settings.

> Name clash: the npm package *claude-code-router* also installs a `ccr` command. ccr
> checks that the `ccr` it finds is this tool before using the bare name.

## Quick start

```sh
cd your-project
ccr init --dry-run          # print the exact diff of every file it would touch; writes nothing
ccr init                    # apply it (every modified file is backed up to .ccr/backups/)
ccr doctor                  # check the setup
ccr explain "Fix the off-by-one in paginate() in app/api/pagination.py"   # preview a decision
claude                      # work as usual
```

You can run `ccr init` again at any time. It keeps your existing settings keys, hooks and any
agent files you edited, and makes no changes when everything is already up to date.

Opt out for a single prompt by adding `!noroute` anywhere in it. A leading `!` is bash mode
in Claude Code, so put it at the end: `rewrite the README intro !noroute`.

## How it works

```
                    plain `claude` session
                              │
 SessionStart ─────► ccr hook session-start   records model, sweeps idle tasks,
                              │               refreshes a stale graph in the background
                              ▼
 UserPromptSubmit ─► ccr hook prompt ──► decide(prompt)                    (rules only, < 0.5 s)
                              │            ├─ classify   task class, scope, vagueness, destructive
                              │            ├─ locate     files/symbols  ◄── .ccr/graph.db (tree-sitter)
                              │            ├─ risk       blast radius, complexity, tests, churn,
                              │            │             critical paths  ◄── CLAUDE.md / memory notes
                              │            ├─ learning   per class x area ◄── .ccr/history.jsonl
                              │            └─ budget     weekly usage     ◄── .ccr/local/state.json
                              │                                                ▲
                              ▼                                                │
                 "ccr: T2 (sonnet/medium) ... Delegate to ccr-implement"       │
                              │                                                │
            main model delegates to .claude/agents/ccr-* (model + effort       │
            per tier), Explore runs on haiku                                   │
                              │                                                │
 Stop / SubagentStop ► ccr hook stop       tokens, subagents, tool errors      │
                              │                                                │
 next prompt ──────► previous task closed: success, or failure on              │
                     "still failing" (and the retry goes one tier up)          │
                              ▼                                                │
                     .ccr/history.jsonl ◄─ ccr feedback good|bad               │
                                                                               │
 status line ──────► ccr statusline ── model, effort, 5h / 7d rate limits ─────┘
```

`ccr run "<task>"` follows the same decision path without an interactive session. It runs
`claude -p` at the routed tier and moves up one tier if the attempt fails
(see [Headless runs](#headless-runs-ccr-run)).

### Tiers

| Tier | Model / effort  | Agent           | Typical work |
|------|-----------------|-----------------|--------------|
| T0   | haiku / low     | `ccr-quick`     | questions, lookups, typos, renames, formatting |
| T1   | sonnet / low    | `ccr-small`     | tests, clear bug fixes, small scoped edits |
| T2   | sonnet / medium | `ccr-implement` | features, refactors, perf work, risky bug fixes |
| T2H  | sonnet / high   | `ccr-senior`    | a single push-up from T2: risky or complex multi-file changes |
| T3   | opus / high     | `ccr-architect` | migrations, security, unclear or intermittent bugs, design and plans |
| T4   | opus / xhigh    | `ccr-critical`  | only by a critical-path rule (see below) |

`Explore` is overridden to run on haiku for search. These are the defaults; change them in
`tiers:` (see the configuration reference).

Routing also handles a few cases that need more than cue words:

- **Design class.** Requests for a plan, strategy or architecture ("design a plan for
  multi-currency support", "how would you structure ...") start at T3 (`class_base.design`).
- **Bug reports that name no code.** A short bug report ccr cannot tie to any file ("it
  crashes", "the weekly report is wrong") is routed as `debugging` (T3) because the work is
  finding the problem. A retry phrase such as "still failing" is not treated this way.
- **Destroying production data.** "delete all ... from the production database", "truncate
  the audit table in prod" and "wipe the users table on prod" go to T4 whatever class they
  were given. A question about the same thing does not.
- **Sweeping rewrites.** "rewrite everything in rust", "make it multi region" and similar get
  two tiers up (capped at T3 unless a critical-path rule applies).
- **Follow-ups.** A short reply that leans on the conversation ("do the same for refunds",
  "make it stricter") inherits the previous task's files for locating, and its class and tier
  when the reply says little about the kind of work. The routing line says so.

How a tier is picked (every number is in the config):

1. Start from the **class base tier**: question and trivial start at T0; tests and bugfix at
   T1; feature, refactor and perf at T2; migration, security and debugging at T3.
2. **+1** for any of: large scope, a vague bug report, a hard problem (including
   intermittent failures: "sometimes", "flaky", "randomly"), complex code (for bugs, perf and
   debugging: high complexity plus many dependents or weak tests), high risk, a critical
   path, or a memory flag (critical, protected, fragile). **+2** when three or more of these
   agree. The push-up is capped by `routing.max_escalation` (2). T2H is a half step: a
   single +1 from T2 lands on it, a +2 or a critical path jumps over it to T3.
3. **-1** for a small scope or explicit step-by-step instructions, but never on a critical
   path, and never below T1 for code changes.
Prompt length is not a signal. The code the task touches is.
4. **Learning** from history (see [Feedback, stats and learning](#feedback-stats-and-learning)).
5. Without a T4 rule the tier never exceeds **T3** (`routing.ceiling_without_critical`).
6. **T4 rules** (critical path):
   - a security change on a critical path;
   - a destructive migration on one;
   - a file that memory marks critical or protected;
   - a critical path with high code risk.
7. **Budget pressure** lowers a non-critical task one tier. Critical and T4 tasks never
   move down.

`ccr explain "<task>"` shows each step with its reasons and scores. It runs nothing.

## Configuration reference (`.ccr/config.yaml`)

`ccr init` creates a short config file and you commit it. Any key you leave out uses the
built-in default. `ccr doctor` validates the file. An invalid value keeps its default, and
hooks stay silent instead of crashing.

Defaults are shown below. The `critical_paths` and `critical_terms` lists are abbreviated
here; the full lists are in `ccr/config.py`.

```yaml
version: 1

tiers:                       # effort: low|medium|high|xhigh (effortLevel does not accept max)
  T0: {model: haiku,  effort: low,    agent: ccr-quick}
  T1: {model: sonnet, effort: low,    agent: ccr-small}
  T2: {model: sonnet, effort: medium, agent: ccr-implement}
  T2H: {model: sonnet, effort: high,  agent: ccr-senior}
  T3: {model: opus,   effort: high,   agent: ccr-architect}
  T4: {model: opus,   effort: xhigh,  agent: ccr-critical}   # models: haiku|sonnet|opus|claude-* ids
                                                             # (fable only on T4). Rerun `ccr init`
                                                             # to regenerate agent files.
class_base:                  # starting tier per class
  question: T0
  trivial: T0
  tests: T1
  bugfix: T1
  feature: T2
  refactor: T2
  perf: T2
  migration: T3
  security: T3
  debugging: T3

critical_paths:              # globs per group; `**` spans dirs, `*` does not; case-insensitive
  auth: ["**/auth/**", "**/login*", "**/session*", "**/jwt*", ...]
  payments: ["**/payment*/**", "**/billing/**", "**/stripe*", "**/ledger*", ...]
  migrations: ["**/migrations/**", "**/*.sql", "**/schema.prisma", ...]
  infra: ["**/terraform/**", "**/*.tf", "**/k8s/**", "**/Dockerfile*", "**/.github/workflows/**", ...]
  secrets: ["**/secret*", "**/crypto*", "**/*.pem", "**/.env*", ...]
  # set a group to [] to disable it; add your own groups freely

critical_terms:              # prompt words/phrases (whole words, simple plurals)
  auth: [auth, login, oauth, jwt, password, "session token", "session fixation", ...]
  payments: [payment, billing, refund, stripe, ledger, ...]
  # ... migrations, infra, secrets

hard_terms: []               # your words for "hard" (like "intermittent"): [cache warm, herd]
pricing:                     # blended USD per Mtok, only for the `ccr stats` savings estimate
  {haiku: 2.0, sonnet: 6.0, opus: 30.0}

risk:
  weights: {blast_radius: 0.6, complexity: 0.5, test_proximity: 0.3, churn: 0.3,
            critical_path: 0.7, memory: 0.7}       # noisy-or contributions
  medium: 0.3                # score >= medium -> "medium"
  high: 0.55                 # score >= high -> "high" (+1 for change tasks)
  t4_code_risk: 0.65         # code-only risk for the T4 rule on a critical path
  blast_full: 32             # dependents that saturate blast radius
  churn_full_commits: 20
  complexity_full_branches: 15

budget:
  soft_limit_pct: 80         # weekly usage % where non-critical tiers drop one level
  state_max_age_hours: 24    # ignore older status line data
  weekly_token_limit: null   # set to enable the token-count fallback

history:                     # learning loop (ccr/history/learn.py)
  min_samples: 3             # K: decided tasks needed before a rule fires
  fail_rate_up: 0.5          # failure rate at the starting tier that moves it up
  success_rate_down: 0.8     # success rate that moves it down / explores one lower
  window_days: 90
  struggle_tool_errors: 5    # a success with >= this many tool errors counts as a failure
  struggle_turns: 30         #   ... or >= this many turns (0 disables each); learning only
  explore_down: true         # try one lower on low-risk simple tasks that keep succeeding

llm:                         # opt-in assist (`ccr explain --llm`, `ccr run`)
  threshold: 0.5             # rule confidence below this may ask the LLM
  model: haiku
  effort: low
  timeout_s: 45

routing:
  max_escalation: 2          # tiers above the class base without a T4 rule
  ceiling_without_critical: T3

run:                         # headless `ccr run`
  test_command: null         # e.g. "pytest -q"; must exit 0 for success
  test_timeout_s: 900
  max_escalations: 1         # tiers to climb after a failed attempt (0 = never retry)
  max_turns: 40              # per attempt
  max_budget_usd: null       # cap for the whole run, across attempts
  timeout_s: 3600            # wall clock per attempt
  permission_mode: null      # passed as --permission-mode (e.g. acceptEdits)

hooks:
  enabled: true              # master switch
  route_prompts: true        # inject the routing line
  show_in_ui: true           # also show a short routing line to you (systemMessage)
  model_advice: true         # suggest /model for iterative work above the main model
  background_graph: true     # refresh a stale graph after SessionStart
  watchdog_ms: 1500          # hard deadline per hook run
  success_gap_minutes: 120   # idle longer than this before the next prompt: outcome unknown
```

`ccr init` also writes the main-session defaults `model` and `effortLevel` into
`.claude/settings.json`, but only when you have not set them already (or with `--force`).
The values come from a profile of the project: haiku/low for small repositories,
sonnet/low otherwise. `ccr profile --apply` sets them explicitly.

## Team workflow

| Path | Commit? | What it is |
|------|---------|------------|
| `.claude/settings.json` | yes | model/effortLevel defaults, hooks, status line (bare `ccr` commands) |
| `.claude/agents/ccr-*.md`, `Explore.md` | yes | tier subagents; your edits are preserved on re-init |
| `.ccr/config.yaml` | yes | tiers, thresholds, critical paths |
| `.ccr/history.jsonl` | yes | shared routing outcomes: everyone's learning improves together |
| `.gitattributes` | yes | `.ccr/history.jsonl merge=union` |
| `.claude/settings.local.json` | no | personal overrides (`--local`, a wrapped user status line) |
| `.ccr/graph.db*` | no | code graph cache, rebuilt per machine |
| `.ccr/local/` | no | session state, status line relay, hook log |
| `.ccr/backups/` | no | backups from `ccr init` |

`ccr init` adds the ignore lines and the `.gitattributes` entry. If `.gitattributes`
already says how `history.jsonl` merges, init leaves your setting alone.

**How history merges.** `history.jsonl` is append-only, and every record has a random id.
With `merge=union`, git merges two branches by keeping both sides' lines, with no conflict.
If a conflict is resolved by hand and both sides are kept, the result can contain git
conflict markers, duplicated lines and lines out of order. All of these are safe:
reading skips lines that are not JSON, counts duplicates once, and sorts by timestamp.
Feedback records apply to their task wherever they sit in the file. `ccr doctor` reports
duplicate and unreadable lines.

## Status line relay and budget pressure

Only Claude Code's status line receives the session's model, effort and
`rate_limits.five_hour` / `seven_day` usage. Hooks do not. `ccr init` therefore installs
`ccr statusline` as the status line command. It writes that data to
`.ccr/local/state.json` and prints a short line:

```
ccr T2 sonnet/medium · 5h 24% · 7d 41%
```

If you already have a status line, ccr wraps it instead of replacing it:
`ccr statusline --wrap "<your command>"` runs your command with the same input and prints its
output unchanged. A user-level status line is wrapped from `settings.local.json`, so the
shared file holds no personal command. `--no-statusline` skips the relay.

The prompt hook uses that state in two ways:

- **Model advice.** For iterative work above the main model (a retry, debugging, a vague
  request, a broad design question), the hint suggests `/model opus` for the conversation
  and `/model <previous>` afterwards. When the main session already runs at the tier, the
  hint says to handle the task directly.
- **Budget pressure.** When weekly usage reaches `budget.soft_limit_pct` (default 80%),
  non-critical tasks drop one tier. Critical-path and T4 tasks never drop. Without status
  line data, the fallback counts tokens from history and the current transcript against
  `budget.weekly_token_limit` if you set one.

## Hooks and fail-open behavior

| Event | Command | Does |
|-------|---------|------|
| SessionStart | `ccr hook session-start` | records the model; closes tasks of idle sessions; refreshes a stale graph in a detached, offline, locked background process |
| UserPromptSubmit | `ccr hook prompt` | routes the prompt and injects one `ccr:` line (`additionalContext`); closes the previous task |
| Stop, SubagentStop | `ccr hook stop` | adds tokens (once per message id), subagent types, tool errors and failure hints to the pending task |

Every hook **fails open**. Bad input, a missing or corrupt config or graph, an exception,
or a run longer than the watchdog (`hooks.watchdog_ms`, 1.5 s; Claude Code's own timeout is
set to 5 s) produces no output and exit code 0. The hook never exits with code 2, so a
prompt is never blocked. Hooks import neither typer nor tree-sitter and only read the
graph. A warm prompt hook takes about 60 ms in-process and about 150 ms as a process.
Problems go to `.ccr/local/hooks.log`. Set `CCR_HOOK_DEBUG=1` to also log input field
names (never their values).

The hook stays silent for:

- `!noroute`;
- slash commands;
- synthetic user turns such as `<task-notification>`;
- short chatter ("thanks", "go on"), which continues the current task;
- `hooks.route_prompts: false` or `hooks.enabled: false`;
- ccr's own `claude -p` calls (`CCR_INTERNAL=1`).

## Headless runs (`ccr run`)

```sh
ccr run "Fix the off-by-one in paginate() in app/api/pagination.py" \
    [--tier T1] [--max-turns 20] [--max-budget-usd 0.50] [--test-command "pytest -q"] \
    [--max-escalations 1] [--permission-mode acceptEdits] [--no-llm] [--json] [--dry-run]
echo "task text" | ccr run -
```

1. **Route** with `decide()`. Here the LLM assist is allowed when rule confidence is low;
   `--no-llm` turns it off. `--tier` overrides the starting tier.
2. **Run** your own `claude -p --model <m> --effort <e> --output-format json --max-turns N
   [--max-budget-usd X]`. The task goes on stdin, never on the command line.
   `CLAUDE_CODE_EFFORT_LEVEL` is removed from the child's environment, and `CCR_INTERNAL=1`
   keeps ccr's hooks quiet inside the run.
3. **Judge the attempt.** Success requires all of: exit code 0; `is_error` false; `subtype`
   `success`; and `run.test_command` exiting 0, if one is configured. These result keys were
   checked against Claude Code 2.1.x: `total_cost_usd`, `usage`, `modelUsage`, `num_turns`,
   `is_error`, `subtype` (`error_max_turns`, `error_max_budget_usd`, ...).
4. **Escalate** after a failure, exactly one tier and never skipping one. Escalation to T4
   happens only when a critical-path rule allows it, and the run stops after
   `run.max_escalations` (default 1). The retry prompt notes why the previous attempt
   failed and includes the end of the failing test output. The working tree keeps whatever
   the earlier attempt changed.
5. **Log** one history record per attempt. The record holds tier, model, effort, outcome,
   reason, turns, tokens, the usage breakdown, per-model tokens and cost, and the test
   result. It never holds the prompt text. An escalated attempt is recorded as a `failure`
   of its tier (`escalated_to`), and the next attempt carries `escalated_from`.

`--max-budget-usd` caps the whole run: each later attempt only gets what is left. If
claude is missing, its output can't be parsed, or the attempt times out, the run stops
with outcome `error` and exit code 2. Moving up a tier would not fix those problems, so
learning ignores them. Exit codes: 0 success, 1 failed after escalation, 2 usage or
infrastructure error. The result text goes to stdout. Progress lines (`ccr: ...`),
including a `claude --resume <session>` hint, go to stderr.

## Feedback, stats and learning

```sh
ccr feedback               # show the last routed task
ccr feedback bad           # it did not work (counts as a failure of its tier)
ccr feedback good --id 9cef  # target a specific task by id prefix
ccr stats [--json] [--days 30] [--misroutes]
```

`ccr feedback` targets the newest task. That can be a `ccr run` attempt, a finished
interactive task, or one **still in progress** in a Claude Code session: the prompt hook
assigns the history id when it routes a task. Feedback is appended as a correction record,
and the newest feedback for a task wins when history is read. History is never rewritten.

Example `ccr stats`:

```
Tasks:     7 (run 7; 4 ccr run(s)) from 2026-10-13 18:25 to 2026-10-13 18:25; 1 corrected by feedback
Usage:     95.0k tokens (run 95.0k); est. cost $0.1909 (claude-reported, headless only)
Outcomes:  3 ok, 4 failed, 0 unknown; success 43%, escalated 3 (43%)
Tier distribution: T0 1 (14%)  T1 3 (43%)  T2 3 (43%)
Per tier:
  tier tasks   ok fail   ? success escal   tokens      cost
  T0       1    1    0   0    100%    0%    37.4k   $0.0719
  T1       3    0    3   0      0%  100%    31.0k   $0.0647
  T2       3    2    1   0     67%    0%    26.5k   $0.0543
Per area:
  area      tasks   ok fail   ? success escal   tokens      cost  tiers
  app/api       6    2    4   0     33%   50%    57.5k   $0.1190  T1:3 T2:3
  app/utils     1    1    0   0    100%    0%    37.4k   $0.0719  T0:1
Per task class:
  ...
Learning (what routing does with this history):
  up      bugfix in app/api: T1 failed 3/3 -> start at T2
```

`ccr stats` also prints two lines that answer "is the router paying off?":

```
Savings:   est. $10.20 routed vs $18.00 all-Opus (43% saved) and $3.60 all-Sonnet (-183%) ...
Misroutes: over-routed 2/40 (5%, a cheaper tier had a proven record), under-routed 3/40 (8%, failed or escalated)
  tune: 2/3 bugfix task(s) failed at T1 with no hard or escalation signal: add project words ...
```

- **Savings** prices each task's tokens at its tier's model (`pricing:`) and again at an
  all-Opus and all-Sonnet baseline. It is an estimate: it assumes the stronger model would
  use the same tokens, so it ignores rework a stronger model might save. A negative saving
  against all-Sonnet is shown, not hidden. Read it next to the success rate.
- **Per success** divides routed cost by the number of successes, so failed tasks (which
  still cost tokens) count against routing. The all-Opus side assumes Opus always succeeds,
  the case most favourable to Opus. A saving that survives this is not just cheaper-and-worse.
- **Over-routed**: the task succeeded above the class floor while the tier below has a
  proven record (`min_samples`, `success_rate_down`) for that class x area.
- **Under-routed**: the task failed or was escalated at the tier it got.
- `--misroutes` lists each one with the rules that fired, from the `trace` in the history
  record (cue words from the built-in rules, the `hard_terms` that matched, and which +1
  signals fired; never the prompt text). The `tune:` lines say which rule to adjust. They
  only suggest: ccr never edits its own rules. A `tune: cue 'x' failed 3/4 at T1` line
  means a cue word's tasks keep failing at the tier it routes to: the rule has drifted.

`ccr dashboard [--days N] [--out FILE] [--open]` writes one self-contained HTML file
(default `.ccr/local/dashboard.html`, no server or network): KPI tiles, tasks per day by tier,
success rate by tier, estimated cost against all-Opus and all-Sonnet, tokens by area, a class
x tier heatmap, misroutes with `tune:` hints, learning adjustments and a
decisions list (paginated, searchable, expandable), with a light/dark toggle. By default it shows
the same data as `ccr stats` and no prompt text. Set `hooks.store_prompts: true` and new tasks also
keep their prompt and the full decision steps (class, files, risk, learning, budget) in
`.ccr/local/prompts.jsonl`. That file stays on your machine (gitignored, never in the shared
`history.jsonl`) and holds your prompt text, so leave the setting off if you don't want that.

**Signals beyond "still failing".** Two more signs that a cheap answer fell short are recorded
on the previous task: `manual_model_up` (you moved the main session to a stronger model, and the
next prompt is about the same code or leans on the last answer) and `possible_rework` (a
dissatisfied reply such as "not quite" or "I meant" about the same code within
`hooks.rework_minutes`). `manual_model_up` counts as a struggle for learning; `possible_rework`
only counts once it repeats for the same class, area and tier. `ccr stats` shows both counts.

`ccr shadow --max-budget-usd N [--from-tier T3] [--shadow-tier T1] [--sample 3] [--test-command CMD] --yes`
measures over-routing directly: it re-runs a few sampled deep-tier tasks (prompts from
`hooks.store_prompts`) at a lower tier with your own `claude -p`, in a throwaway `git worktree`
of HEAD, and records whether they succeeded in `.ccr/local/shadow.jsonl`. It spends real usage,
so it prints the plan and runs nothing without `--yes` and a budget cap. Without
`--test-command`, "success" only means claude exited cleanly. Results appear in `ccr stats` and
the dashboard.

`ccr tune [--apply]` turns misroute evidence (drifting cue words, classes that keep failing or
keep being over-routed) into a diff of `.ccr/config.yaml` (`hard_terms`, `class_base`). It
changes nothing unless you pass `--apply`, which backs the file up and refuses an invalid result.

`ccr export-benchmark [--out FILE]` turns decided tasks that have a stored prompt
(`hooks.store_prompts`) into your own replay set for `bench_routing.py --prompts`. A failure at a
tier labels the prompt "needs the next tier up"; a success labels the lowest tier proven for that
class and area. Successes prove a tier was enough, not that it was necessary, so this catches
under-routing well and over-routing only where a cheaper tier has its own record.

`ccr calibrate` fits a small logistic regression (stdlib only) of failure odds on the routing
signals in each record's trace and the tier it got. It reports cross-validated AUC, a verdict, the
strongest signals and a shadow comparison (the tier the model would have picked). It needs 30
decided tasks with at least 5 successes and 5 failures, and says "no better than guessing" when the
signals don't predict failures. It never changes routing: use it to decide which rules to tune.

`uv run python scripts/bench_routing.py` replays a labelled prompt set (terse but huge,
long but trivial, "fix login sometimes failing", ...) and reports under- and over-routed
prompts and the estimated cost against the baselines, without API calls. Edit `PROMPTS`, or pass
`--prompts FILE.json` (a list of `[prompt, need_tier, cap_tier]`, tiers 0-4) for your own work.

How the columns are computed:

- **Success rate** is successes / (successes + failures). Tasks whose outcome is unknown
  are left out.
- **Escalation rate** is the share of a tier's tasks that were retried one tier higher.
- **Tokens** count input + output + cache-creation tokens.
- **Cost** is what `claude -p` reports (an estimate). Interactive subscription sessions
  have no per-task price, so they show tokens only.

**How an interactive outcome is judged** (when the next prompt arrives): a retry phrase
("still failing") is a failure; a revert phrase ("undo that", "start over") is `abandoned` and
not counted; going idle for longer than `hooks.success_gap_minutes` leaves it unknown; anything
else is a success. A success that struggled (at least `history.struggle_tool_errors` tool errors,
`history.struggle_turns` turns, or the model said it could not finish) is counted as a failure by
the learning rules only. `ccr stats` still shows the real outcome and how many struggled.

**Learning rules** (per task class x area; class-wide when that area has no samples yet):

- **up**: the starting tier failed at least `fail_rate_up` of at least `min_samples`
  decided tasks. Start one tier higher, within the escalation cap.
- **down**: the tier below succeeded at least `success_rate_down` of at least
  `min_samples`. Start one tier lower.
- **explore**: the starting tier itself keeps succeeding (at least `success_rate_down` of
  at least `min_samples`), the task is low-risk and simple, and the tier below has no
  recorded failure. Try one tier lower. If that tier then fails, the *up* rule moves the
  task back, so the loop settles.
- History never lowers a task on a critical path, or below the class floor.

Each adjustment cites its counts. They appear on the `Learning:` line of `ccr explain`
(for example, `T1 failed 3/3 for bugfix in app/api: +1`) and in `ccr stats`.

Interactive outcomes are inferred: the next prompt closes the previous task as a success
unless it is a retry phrase ("still failing", "didn't work", "same error"). A retry marks
a failure and routes one tier higher. Use `ccr feedback` when that inference is wrong.

## Privacy

- **No prompt text is stored.** History, session state and logs keep only routing
  metadata, whitelisted field by field: class, area (a directory prefix), tier, model,
  effort, outcome, token counts, cost, ids. Hooks scan the last assistant message for a
  failure hint and drop it. Tests check this with sentinel strings.
- **No API keys, no SDK.** The only model calls go through your own `claude` CLI:
  - the opt-in LLM classification: `ccr explain --llm`, and `ccr run` unless you pass
    `--no-llm`. It runs haiku/low with no tools, no MCP and no session, and is only asked
    when rule confidence is below `llm.threshold`;
  - the `ccr run` task itself.
  Hooks never call a model.
- Grammar downloads (tree-sitter-language-pack) are the only network access ccr itself
  makes.

## Coexistence with other routers

ccr only adds its own pieces:

- `ccr-*` agents and an `Explore` override;
- hooks that it tags and later recognizes;
- a wrapped status line;
- `model` and `effortLevel` defaults, only where none exist.

Your hooks, agents and settings keys are kept.

Other routing instructions still apply. A user-global `~/.claude/CLAUDE.md` might say
"delegate lookups to `quick`, features to `standard`". The main model then sees both
those rules and ccr's hint, and either could win. The same is true inside `ccr run`:
`claude -p` loads your global CLAUDE.md, and a haiku T0 run may still delegate to your
own subagents. ccr does not edit `~/.claude`. To compare like with like, remove or narrow
the other rules while you evaluate ccr, or point them at the `ccr-*` agents.

## Limitations

- **Delegation is advisory.** The hook can only add context. The main model decides
  whether to delegate to the suggested agent, and may not.
- **The main model can't be switched automatically.** No hook can change the session
  model, so ccr suggests `/model` for iterative work above it. Subagents do get their
  tier's model and effort from their frontmatter.
- **The status line doesn't run in `-p` mode.** `ccr run` and other headless sessions use
  the last interactive state (up to `state_max_age_hours` old) or the token fallback for
  budget pressure.
- **Grammars download on first use.** tree-sitter grammars are fetched the first time a
  language is parsed (`ccr init` / `ccr graph`). Offline, or for unsupported languages
  (e.g. Zig), ccr falls back to regex extraction. Hooks and the background refresh never
  download.
- **The call graph is heuristic.** Calls are resolved by name, imports and scope, not by
  types, so dynamic dispatch, reflection and generated code are approximate. Blast radius
  is an estimate.
- **The classifier is keyword-based.** Unusual wording can land in the wrong class; the
  confidence score and `--llm` exist for that. The word "session" alone does not count as
  an auth term ("in this session" is just conversation). Security phrases such as
  "session token" or "session fixation" do.
- **`effortLevel` does not accept `max`.** Only low, medium, high and xhigh are valid.
  Subagent frontmatter accepts `max`, but ccr keeps the tiers within the `effortLevel`
  range. Never set `CLAUDE_CODE_EFFORT_LEVEL`: it overrides every subagent's effort, and
  `ccr doctor` warns when it is set.
- **Costs are estimates.** `--max-budget-usd` and `total_cost_usd` are Claude Code's
  client-side estimate. They work as a safety cap, not as billing.
- **Outcome inference is heuristic.** It relies on retry phrases, tool errors and
  feedback. A task you abandon without saying anything counts as a success.
- **Windows is untested locally.** Command quoting covers Git Bash and PowerShell, but the
  suite has only been run on macOS.

## Troubleshooting

Start with `ccr doctor`. It checks:

- the `claude` binary and version;
- `CLAUDE_CODE_EFFORT_LEVEL`;
- that settings are valid JSON and ccr's hooks are wired;
- that the config is valid;
- graph freshness;
- memory sources;
- history health: duplicate or unreadable lines, and the union merge setting.

`ccr doctor --verbose` also writes the results to `.ccr/local/ccr.log`.

| Symptom | Try |
|---------|-----|
| No routing line shown in the UI | `hooks.show_in_ui` must be true (default). It is emitted as the hook's `systemMessage`; the interactive CLI shows it as a hook notice, while the VS Code panel may not render it. Silent prompts (chatter, `!noroute`, slash commands) show nothing |
| No `ccr:` line in Claude's context | `ccr doctor`. Check that `.ccr/` exists at the project root, `hooks.enabled` and `route_prompts` are on, the prompt has no `!noroute`, and `.ccr/local/hooks.log` shows no errors or watchdog overruns |
| Hooks call an absolute Python path | install ccr on PATH (`uv tool install ...`), then rerun `ccr init` |
| Routing ignores files you changed | `ccr graph` (incremental) or `ccr graph --rebuild` |
| Wrong tier | `ccr explain "<task>"` shows every reason; adjust `class_base`, `critical_paths` or the risk thresholds, or use `ccr feedback bad` |
| Budget pressure never applies | the status line must be ccr's or a wrapped one; check that `.ccr/local/state.json` updates |
| `ccr run` exits 2 | claude not found, unparseable output or a timeout; run the printed command by hand |
| Merge left odd lines in history | harmless; `ccr doctor` counts them, reading ignores them |

## Development

```sh
uv sync
uv run pytest            # full suite, including hook latency and a fake-claude escalation test
uv run ruff check
uv run ccr --help
```

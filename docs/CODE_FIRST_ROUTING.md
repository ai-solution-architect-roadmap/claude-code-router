# Code-first routing

Status: steps 1, 2 and 3 are always on since 0.2.1 (they were opt in in 0.2.0). Step 4 is
designed here and comes next.

## Problem

Today the tier starts from the prompt's wording. `ccr/evaluate/classify.py` reads keywords
("fix", "refactor", "migration") and picks a class base tier; the code graph can then move it
by at most two tiers. Files are found by matching prompt words against paths and symbols.
That works when the prompt describes the work well and fails when it does not: "finish this"
names no file, "just a small fix" can sit in a file forty modules import, and "refactor the
helper" can be one line.

The goal is to decide from the files and the change instead. The catch is timing: when the
prompt hook runs, no change exists yet. So the router has to predict the change from the code
it can see, and then check the prediction against the real edits once Claude starts working.

## Flow

```
prompt ─▶ 1 find the real files ─▶ 2 predict the change (optional) ─▶ 3 decide from the code
                                                                          │
                       history, T4 rules, budget (unchanged) ◀────────────┘
                                         │
                                         ▼
                                  tier T0..T4 ─▶ Claude edits ─▶ 4 check the real change
                                                                    │
                                     record predicted vs actual ◀───┘ (tunes step 3)
```

## Step 1: find the real files (built)

`ccr/evaluate/workspace.py` takes a snapshot of the repository state with three short git
calls (about 10 ms on this repo, each with a 1 s timeout, empty outside git):

- uncommitted changes: `git diff HEAD --numstat` (lines added plus deleted per file);
- untracked files: `git ls-files -o --exclude-standard` (lines counted);
- the files of the last `code_first.recent_commits` commits (default 5).

`locate()` takes the snapshot as a fifth evidence stage, after paths, symbols, words and
memory. Evidence combines noisy-or as before:

| Situation | Evidence |
| --- | --- |
| the prompt points at a file you are editing | +0.5 "uncommitted edit" |
| the prompt names no code at all, and 10 or fewer files are edited | +0.5/√n on each edited file |
| an edited file sits next to a file the prompt points at | +0.2 |
| the prompt points at a file changed in a recent commit | +0.25 |

So "finish this and make it work" while you have `app/reports/export.py` open and edited now
locates that file, and a prompt that names a file you are already changing becomes certain.
More than 200 changed files (a mass rewrite or a generated tree) are ignored as a signal.

## Step 2: predict the change (built)

`ccr/evaluate/predict.py`, always on. It asks Haiku through your own
`claude` CLI (no API key), the same way `ccr explain --llm` does.

**When it asks.** Only for real code changes (tests, bugfix, feature, refactor, perf,
migration, security, debugging) that some file was located for, with no T4 rule, and only
when routing is unsure:

- overall confidence below `llm.threshold` (0.5);
- a code score within `predict.boundary` (0.08) of a band edge;
- or keywords and code disagreeing by two tiers or more.

Questions, design, trivial edits and confident routes never wait.

**What it sends.** The prompt, the top 5 located files with their symbol outlines from the
graph (kind, name, line span; no source), and the uncommitted diff stat.

**What it gets back.** One JSON object: files with confidences, estimated lines, reach
(`one_function`, `one_file`, `few_files`, `cross_cutting`), whether an interface or a schema
changes, a confidence and a short reason. Predicted files that do not exist are dropped,
unless a new file sits in a located directory; if every file was invented, its confidence is
halved.

**How it moves the tier.** The answer becomes a size:

| Size | When |
| --- | --- |
| large | schema change, cross-cutting, 4+ files, or 300+ lines |
| small | one function or one file, 1 file, up to 30 lines, no interface change |
| medium | everything else |

Small lowers the tier by one, large raises it by one, medium leaves it. Limits:

- below `predict.min_confidence` (0.6) the prediction is shown but not used;
- the result stays within one tier of the keyword route, so it never stacks on a code raise,
  and a small prediction can cancel one;
- it never lowers a critical path, perf work (a small patch says nothing about the measuring
  it takes to find it), or a code change below T1;
- T4 rules, history, the ceiling and budget pressure still apply after.

**Speed and cost.** The hard limit is `predict.timeout_s` (10 s, at most 13); a late or broken answer
leaves routing as it was and adds a note. The prompt hook's watchdog grows to the timeout
plus 1 s when prediction is on. Answers are cached in `.ccr/local/predict-cache.json` (300
entries, answers only, no prompt text) keyed by the prompt, the located files, the
uncommitted diff and HEAD. Measured on this container, the CLI takes 4 to 6.8 s per call
(about 0.85 s of that is Haiku; the rest is CLI start-up), at about $0.0005 a call. The
predictor starts the CLI lean: without project settings (and so without their hooks) or
slash commands, which saves about 0.4 s, or in minimal `--bare` mode when
`ANTHROPIC_API_KEY` is set (about 0.7 s in total). The first version used a 5 s limit, and
Claude Code's own 5 s hook timeout cut calls off too; on the 48-prompt probe 4 calls timed
out and repeated runs disagreed. `ccr init` now gives the prompt hook a 15 s Claude Code
timeout; re-run it after turning prediction on.

Starting the call earlier in the hook was considered: routing before the call takes about
60 to 150 ms, so overlapping it would save under 0.2 s of a 4 s call, and the call can only
be built once routing has located the files and decided it is unsure.

A large prediction does not raise a task whose keyword route already rose for complex,
risky or critical code, the same no-double-count rule the code score follows. This is the likely
cause of "improve the error handling in ccr/cli.py" reaching T3 on the probe: its keyword
route had already risen for complex code. Four fresh Haiku calls on it here all answered
"one file, 60 to 90 lines" (medium), so the T3 came from a rarer large answer stacking on
that rise, which this rule now blocks.

### Results with real Haiku

On ccr's own code (17 probe prompts, including the 11 that the first code-first version made
worse): asked on 9, changed 5.

| Prompt | Main | Code-first | + prediction |
| --- | --- | --- | --- |
| fix the flaky test in tests/test_history.py | T3 | T3 | T2H (target) |
| use pydantic to validate the config in ccr/config.py | T2H | T2H | T2H, matches the other wording (target) |
| fix the null check in ccr/config.py (3 wordings) | T2 | T2 | T1 (a 4 to 6 line guard in one function) |
| just make a small fix to the budget rounding | T2 | T2 | T1 |

On the 39 fixture samples: asked on 15, changed 2 against code-first alone.

| Prompt | Expected | Code-first | + prediction |
| --- | --- | --- | --- |
| Fix the bug where formatPrice shows NaN for zero amounts | T1 | T2 | T1 (better) |
| Fix the data race in the LRU cache | T3 | T3 | T2H (worse: Haiku sees a one-lock fix) |

## Step 3: decide from the code (built)

`ccr/route/code.py` scores the located code. Components, each 0..1, combined noisy-or:

| Component | Weight | Source |
| --- | --- | --- |
| files touched (non-test, confidence ≥ 0.4) | 0.5 | `log2(n)/3`: 1 file 0, 2 files .33, 8+ files 1 |
| change size (lines already changed in them) | 0.4 | uncommitted diff, saturates at 400 lines |
| blast radius × reach | 0.6 | dependents from the graph |
| complexity | 0.5 | graph |
| missing tests | 0.25 | graph |
| churn | 0.2 | git history |
| critical path | 0.75 | `critical_paths` globs |
| memory flags | 0.6 | CLAUDE.md and other memory |

Bands (`code_first.bands`, default `[0.2, 0.4, 0.6]`) map the score to T1, T2, T2H or T3.
Within `code_first.tie_margin` (0.04) of a band edge, the class's usual starting tier picks
the side.

**How the code tier is used: it only raises real code changes.** Keyword routing runs first,
unchanged. Then the code tier can lift the task by one tier, when all of these hold:

- the task is a real code change (tests, bugfix, feature, refactor, perf, migration,
  security, debugging). Trivial edits, questions and design requests keep their keyword tier:
  a typo fix in a complex file is still a typo fix;
- the code tier is above the keyword tier;
- the prompt has no explicit steps and no small-scope words ("just", "small"), which still
  lower the tier as today;
- the keyword route did not already rise for complex, risky or critical code, so the same
  code is not counted twice.

The tier never goes down because of the code. T4 rules, history, the T3 ceiling and budget
pressure apply as today.

`REACH` scales blast radius by how far an edit of the task's kind reaches until step 2
predicts it: tests 0.3, bug fixes, perf and debugging 0.6, features, refactors, migrations
and security 1.0, and 1.0 for any task whose wording says "everywhere".

**When the code is not used.** The keyword tier is kept, with a "code-first: keyword tier
kept" reason, when the task is trivial or does not edit code, there is no code graph, or
location confidence is below `code_first.min_confidence` (0.5).

### History of this rule

The first version let the code score set the tier outright. On a 48-prompt probe against
ccr's own code it moved 15 tiers, and 11 of those got worse: "fix the typo in the error
message in ccr/cli.py", "add a comment", "rename decide" went T0 to T2H, null-check fixes in
`ccr/config.py` went to T3, and explicit steps stopped lowering the tier. Those prompts are
now regression tests (`tests/test_code_first.py::PROBE`). The raise-only rule lost the two
cases that improved; step 2 brings them back.

On the 39 fixture table cases, the raise-only rule moves 2 tiers, both up by one:
"Fix the bug where formatPrice shows NaN for zero amounts" (T1 to T2, a widely imported
helper) and "Add an optional icon prop to the Button component" (T2 to T2H, 3 files, no tests).

## Step 4: check the real change (next)

A `PostToolUse` hook on Edit, Write and MultiEdit, reusing the same snapshot code:

1. After each edit, compare the real diff with the prediction (files and lines).
2. If the change grew past the next band edge, or touched a critical-path file or a file
   memory flags, add a short note suggesting the next tier (the same wording as today's
   retry escalation), at most once per task.
3. The Stop hook records predicted files, actual files, predicted score and actual score in
   the history record (numbers and counts only, no paths, like `trace()`).
4. `ccr calibrate` reads those records to suggest new band edges and weights.

The hook must stay well under the 1.5 s watchdog: one `git diff --numstat`, no graph rebuild.

## Configuration

```yaml
code_first:             # always on; an old `enabled` key is ignored with a warning
  recent_commits: 5     # files from the last N commits strengthen a match
  min_confidence: 0.5   # location confidence needed to route from the code
  bands: [0.2, 0.4, 0.6]   # code score edges: T1 | T2 | T2H | T3
  tie_margin: 0.04      # near an edge, the keyword class breaks the tie

predict:                # step 2, always on; needs your claude CLI, else it is skipped
  model: haiku
  effort: low
  timeout_s: 10         # hard limit (max 13); a late answer leaves routing as it was
  min_confidence: 0.6   # below this the prediction is shown, not used
  boundary: 0.08        # code score this close to a band edge counts as unsure
```

**Fallbacks.** No claude CLI, a timeout, a bad answer or no time left: routing keeps its
rule-based tier and the hook stays quiet (`ccr explain` shows a note). An install from before
0.2.1 gives the prompt hook only 5 s in Claude Code, so the hook shrinks the prediction to fit
(3 s) and `ccr doctor` asks you to re-run `ccr init`, which sets 15 s. Test suites set
`CCR_NO_PREDICT=1` so they never reach the real CLI.

`ccr explain "<task>"` shows the code score, its top components and whether it raised the
tier, or why the keyword tier was kept.

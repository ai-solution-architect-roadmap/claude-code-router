# Code-first routing

Status: steps 1 and 3 prototyped behind `code_first.enabled` (off by default). Steps 2 and 4
are designed here and come next.

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

## Step 2: predict the change (next)

A cheap Haiku call, off by default, with a short time limit (target under 1.5 s, the hook's
watchdog). Input: the prompt, the top located files' outlines from the graph (symbols and
signatures, not full source), and the uncommitted diff stat. Output, as JSON:

- the files likely to change, with a confidence each;
- rough size (lines) per file;
- whether a public interface, schema or config contract changes.

The answer replaces the `REACH` factor in step 3 (see below) with a real prediction, and adds
predicted files to the candidate set the way the existing `ccr explain --llm` hint does. On a
timeout or a bad answer, step 3 runs on step 1 alone.

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

What the prompt's wording still does:

- **Decides whether the task edits code.** Questions and design requests keep keyword routing.
- **Says how far an edit reaches** until step 2 predicts it (`REACH`): a typo fix in a module
  forty files import does not touch those forty files, a refactor may. Trivial and test work
  count 0.3 of the blast radius, bug fixes, perf and debugging 0.6, features, refactors,
  migrations and security work 1.0. Words like "everywhere" set it to 1.0.
- **Breaks ties.** Within `code_first.tie_margin` (0.04) of a band edge, the class's usual
  starting tier picks the side.
- **Sets a floor one tier below the class's usual start**, so a debugging hunt in a small
  file still gets T2H.
- **T4 rules** (security on a critical path, destructive migrations, production wipes) are
  unchanged.

The wording signals of keyword routing (large scope, small scope, hard problem, vague bug,
explicit steps) are not applied when the code decides. History, the T3 ceiling without a T4
rule, and budget pressure apply after, as today.

**Fallback.** Keyword routing runs unchanged, with a "code-first: fell back" reason, when:
the task does not edit code, there is no code graph, or location confidence is below
`code_first.min_confidence` (0.5).

### What changes on the fixture repos

On the 39 table cases in `tests/test_route_table.py`, 9 move:

| Task | Keywords | Code-first | Why |
| --- | --- | --- | --- |
| Refactor Engine.run into smaller functions | T2H | T3 | complexity .77, memory says legacy |
| Fix the retry bug in the Stripe client | T2 | T3 | payments path, 4 files, no tests |
| Add an optional icon prop to Button | T2 | T2H | 3 files, no tests, tie went down |
| Fix formatPrice NaN for zero amounts | T1 | T2 | widely imported helper |
| Small fix to Footer.tsx | T1 | T2 | no tests; "small" no longer lowers it |
| Fix the data race in the LRU cache | T3 | T2H | one simple file; floor holds T2H |
| Terraform: add an S3 bucket | T3 | T2H | critical path, otherwise one simple file |
| Why does memory usage keep growing | T3 | T2H | floor holds T2H |
| Rename Helper everywhere | T1 | T2 | "everywhere" counts every dependent |

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
code_first:
  enabled: false        # opt in
  recent_commits: 5     # files from the last N commits strengthen a match
  min_confidence: 0.5   # location confidence needed to route from the code
  bands: [0.2, 0.4, 0.6]   # code score edges: T1 | T2 | T2H | T3
  tie_margin: 0.04      # near an edge, the keyword class breaks the tie
```

`ccr explain "<task>"` shows the code score, its top components and the tier it picked, or why
it fell back.

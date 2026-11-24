# Changelog

All notable changes to this project are documented here.

## [Unreleased]

## [0.2.1] - 2026-11-24

### Changed
- Code-first routing and the Haiku change prediction are always on; `code_first.enabled` and
  `predict.enabled` are gone (an old setting is ignored with a warning). Without the `claude`
  CLI, or when Haiku is late, routing silently keeps its rule-based tier.
- Recent commits only strengthen a file match that already stands on its own, and are ignored
  when they touch more than 40 files (an initial import), so a fresh repo no longer turns a
  loose word match into a critical path.
- A follow-up prompt waits for Haiku at most once.

### Added
- The prompt hook keeps the prediction inside Claude Code's installed hook timeout, and
  `ccr doctor` warns when that timeout is under 15 s (re-run `ccr init`).
- `CCR_NO_PREDICT=1` keeps test suites from calling the real CLI.
- Releases are automatic: a push to main that changes the version tags it, creates the GitHub
  release from this changelog, and publishes to PyPI and then npm. Steps already done are skipped.

### Fixed
- The npm package name in the repo and README is now `@satya-prakash/claude-code-router`, the
  name 0.2.0 was published under.

## [0.2.0] - 2026-11-24

### Added
- Code-first routing behind `code_first.enabled` (off by default): uncommitted edits and
  recent commits help locate the files, and a score of that code can raise a real code change
  by one tier. Trivial edits, questions, steps and small-scope wording keep their keyword tier.
- Haiku change prediction behind `predict.enabled` (off by default): when routing is unsure,
  Haiku predicts the files, size and contract changes, and the tier moves by one at most. See `docs/CODE_FIRST_ROUTING.md`.
- npm launcher package `@satya-prakash/claude-code-router` (in `npm/`): installs `ccr` with npm and
  runs the matching PyPI release from a private Python environment.
- Release workflow publishes to npm after PyPI and checks that the tag matches both versions.

### Fixed
- Routing inconsistencies between near-identical prompts (found with a 48-prompt probe set).

## [0.1.1] - 2026-10-17

### Added
- Published on PyPI as `claude-code-router` (the command is still `ccr`).
- CI, contributing guide, issue and PR templates, README screenshots and badges.

## [0.1.0] - 2026-10-17

### Added
- Rule-based routing of Claude Code tasks to six tiers (T0 to T4, plus T2H) with matching
  `ccr-*` subagents.
- Task classification, tree-sitter code graph, and a risk score from dependents, complexity,
  tests, churn, critical paths and memory notes.
- Learning from past outcomes, weekly budget pressure and an opt-in haiku assist.
- Commands: `init`, `profile`, `explain`, `run`, `stats`, `dashboard`, `export-benchmark`,
  `calibrate`, `shadow`, `tune`, `purge`, `feedback`, `graph`, `doctor`.
- Claude Code hooks and a status line relay, all fail-open.
- MIT license.

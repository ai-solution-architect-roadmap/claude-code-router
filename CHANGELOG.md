# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- Code-first routing behind `code_first.enabled` (off by default): uncommitted edits and
  recent commits help locate the files, and a score of that code sets the tier, with keywords
  only breaking ties. See `docs/CODE_FIRST_ROUTING.md`.

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

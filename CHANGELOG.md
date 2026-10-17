# Changelog

All notable changes to this project are documented here.

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

# Contributing

Thanks for helping improve ccr.

## Setup

```sh
git clone https://github.com/ai-solution-architect-roadmap/claude-code-router.git
cd claude-code-router
uv sync
```

## Before you open a pull request

```sh
uv run ruff check .
uv run pytest -q
uv run python scripts/bench_routing.py   # if you changed routing rules
```

## Guidelines

- Keep routing rule-based, local and token-free. The only model call allowed is the opt-in
  haiku assist.
- Hooks must fail open: bad input or an exception means no output and exit 0.
- Add a test for every behavior change. Routing changes should also add a prompt to
  `scripts/bench_routing.py`.
- Update the README or `docs/REFERENCE.md` when commands, options or config keys change.
- Keep commits focused, with a short imperative subject line.

## Reporting bugs

Use the issue templates. For a routing problem, include `ccr explain "<prompt>" --json`.

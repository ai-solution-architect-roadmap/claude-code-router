"""Console entry point: `ccr hook ...` and `ccr statusline` take a fast path that never
imports typer (or tree-sitter); everything else goes to the typer app in ccr.cli."""
from __future__ import annotations

import sys

FAST = ("hook", "statusline")


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "hook":
        from ccr.hooks import main as hook_main
        raise SystemExit(hook_main(args[1:]))
    if args and args[0] == "statusline":
        from ccr.statusline import main as sl_main
        raise SystemExit(sl_main(args[1:]))
    from ccr.cli import app
    app(args=args, prog_name="ccr")


if __name__ == "__main__":
    main()

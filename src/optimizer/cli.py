"""Command-line entry point: `uv run optimizer <command>`.

Subcommands are added milestone by milestone (run, report, ...).
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optimizer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", help="Call Haiku twice and print prompt-cache stats")

    args = parser.parse_args(argv)
    if args.command == "smoke":
        from optimizer.smoke import run_smoke  # imported lazily: needs network + API key
        try:
            return run_smoke()
        except RuntimeError as e:  # setup problems: print one clear line, no traceback
            print(f"error: {e}", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

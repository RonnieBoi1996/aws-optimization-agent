"""Command-line entry point: `uv run optimizer <command>`.

Subcommands are added milestone by milestone.
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="optimizer")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", help="Call Haiku twice and print prompt-cache stats")
    run = sub.add_parser("run", help="Analyze the account and write proposals to out/ (shadow mode)")
    run.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                     help="Write outputs to out/ only (default, and the only mode until M5)")
    run.add_argument("--no-dry-run", dest="dry_run", action="store_false",
                     help="Open PRs (not available until M5)")

    args = parser.parse_args(argv)
    try:
        if args.command == "smoke":
            from optimizer.smoke import run_smoke  # lazy: needs network + API key
            return run_smoke()
        if args.command == "run":
            if not args.dry_run:
                print("error: opening PRs arrives in M5; only --dry-run is available.", file=sys.stderr)
                return 2
            from optimizer.run import run_shadow
            return run_shadow()
    except RuntimeError as e:  # setup problems (credentials, identity, budget): one clear line
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

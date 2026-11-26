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
                     help="Open PRs for every proposal (needs the M7 human gate; use `optimizer pr` until then)")
    run.add_argument("--no-synth", dest="synthesize", action="store_false",
                     help="Skip writing Terraform changes (saves one Sonnet loop per proposal)")
    pr = sub.add_parser("pr", help="Open a GitHub PR for ONE synthesized change you picked")
    pr.add_argument("--run-id", required=True)
    pr.add_argument("--proposal", required=True, help="Proposal ID, e.g. cost-<run_id>-001")

    ev = sub.add_parser("eval", help="Score a shadow run against the seeded sandbox (evals/cases.yaml)")
    ev.add_argument("--run-id", help="Run to score (default: the newest in out/)")
    ev.add_argument("--run", dest="run_first", action="store_true",
                    help="Do a fresh shadow run first, then score it (costs a few cents)")

    args = parser.parse_args(argv)
    try:
        if args.command == "smoke":
            from optimizer.smoke import run_smoke  # lazy: needs network + API key
            return run_smoke()
        if args.command == "run":
            if not args.dry_run:
                print("error: opening PRs for a whole run waits for the M7 human gate. Review "
                      "out/<run_id>/summary.md, then: optimizer pr --run-id <run> --proposal <id>",
                      file=sys.stderr)
                return 2
            from optimizer.run import run_shadow
            return run_shadow(args.synthesize)
        if args.command == "pr":
            from optimizer.run import open_pr
            return open_pr(args.run_id, args.proposal)
        if args.command == "eval":
            from optimizer.evals import run_eval
            return run_eval(args.run_id, args.run_first)  # exit 1 = a false positive or a miss
    except RuntimeError as e:  # setup problems (credentials, identity, budget): one clear line
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

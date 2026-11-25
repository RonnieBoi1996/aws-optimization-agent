#!/usr/bin/env python3
"""Generate pipelines/workflows/ (for infra-live) from .github/workflows/.

Run after changing either workflow:  python3 pipelines/sync.py
tests/ci/test_pipelines_in_sync.py fails if you forget.

The two repos' workflows are identical except for the repo-specific settings
(which stacks exist, which ones CI may apply, the state key prefix).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / ".github" / "workflows"
DST = ROOT / "pipelines" / "workflows"

# (exact text in this repo's workflow, replacement for infra-live)
REPLACEMENTS = {
    "terraform-pr.yml": [
        ('STACK_ARGS: "--stack-dirs infra sandbox"', 'STACK_ARGS: "--stack-root stacks"'),
        ('STATE_KEY_PREFIX: ""', 'STATE_KEY_PREFIX: "infra-live/"'),
    ],
    "terraform-apply.yml": [
        ('APPLY_STACK_ARGS: "--stack-dirs sandbox"', 'APPLY_STACK_ARGS: "--stack-root stacks"'),
        ('STATE_KEY_PREFIX: ""', 'STATE_KEY_PREFIX: "infra-live/"'),
        ("# This repo applies sandbox/ only. infra/ is applied by hand (see infra/README.md):\n"
         "# tf-apply is denied writes to the optimizer's own IAM, so it can't apply infra/.\n",
         "# infra-live: every stack under stacks/ is applied by CI. IAM roles created here\n"
         "# must use path /workload/ and the workload-boundary permission boundary.\n"),
    ],
}

HEADER = ("# GENERATED from aws-optimization-agent/.github/workflows by pipelines/sync.py.\n"
          "# Copy into infra-live/.github/workflows/. Don't edit this copy by hand.\n\n")


def render(name: str) -> str:
    text = (SRC / name).read_text(encoding="utf-8")
    for old, new in REPLACEMENTS[name]:
        if old not in text:
            raise SystemExit(f"{name}: expected text not found, update pipelines/sync.py:\n{old}")
        text = text.replace(old, new)
    return HEADER + text


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    for name in REPLACEMENTS:
        (DST / name).write_text(render(name), encoding="utf-8")
        print(f"wrote pipelines/workflows/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

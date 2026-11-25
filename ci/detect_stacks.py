#!/usr/bin/env python3
"""Work out which Terraform stack directories a PR or push changed.

Stdlib only, so it runs on a bare GitHub runner. Used by both repos:

  this repo:   --stack-dirs infra sandbox
  infra-live:  --stack-root stacks          (every stacks/<name>/ is a stack)

Writes `stacks` (JSON list of {"dir", "slug"}) and `any` (true/false) to
$GITHUB_OUTPUT when --github-output is given, otherwise prints the JSON.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

# Files that change what Terraform does in the stack they live in.
TF_FILE_PATTERNS = ("*.tf", "*.tfvars", ".terraform.lock.hcl")

# Changes to these re-check EVERY stack (shared lint config, pipeline itself).
DEFAULT_ALL_STACK_TRIGGERS = (
    ".tflint.hcl",
    ".github/workflows/terraform-*.yml",
    "ci/*",
)

NULL_SHA = "0" * 40  # GitHub's "before" for a brand-new branch


def slug(stack_dir: str) -> str:
    """Artifact and concurrency names can't contain '/'."""
    return stack_dir.strip("/").replace("/", "-")


def discover_stacks(stack_dirs: list[str] | None, stack_root: str | None,
                    repo_root: Path) -> list[str]:
    if stack_dirs:
        return sorted(d.strip("/") for d in stack_dirs)
    root = repo_root / (stack_root or "")
    return sorted(
        str(p.relative_to(repo_root)) for p in root.iterdir()
        if p.is_dir() and any(p.glob("*.tf"))
    )


def stacks_for_changes(changed: list[str], stacks: list[str], modules_dir: str | None,
                       all_triggers: tuple[str, ...]) -> list[str]:
    hit: set[str] = set()
    for path in changed:
        if any(fnmatch(path, pat) for pat in all_triggers):
            return list(stacks)
        name = Path(path).name
        if not any(fnmatch(name, pat) for pat in TF_FILE_PATTERNS):
            continue
        if modules_dir and path.startswith(modules_dir.rstrip("/") + "/"):
            return list(stacks)  # a shared module may be used by any stack
        for stack in stacks:
            if path.startswith(stack + "/"):
                hit.add(stack)
    return sorted(hit)


def changed_files(base: str, head: str, three_dot: bool) -> list[str]:
    rng = f"{base}...{head}" if three_dot else f"{base}..{head}"
    out = subprocess.run(["git", "diff", "--name-only", rng],
                         check=True, capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", required=True, help="base SHA (PR base, or push 'before')")
    p.add_argument("--head", required=True)
    p.add_argument("--mode", choices=["pr", "push"], default="pr",
                   help="pr: diff against the merge base (...); push: plain range (..)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--stack-dirs", nargs="+")
    group.add_argument("--stack-root")
    p.add_argument("--modules-dir", default="modules")
    p.add_argument("--no-pipeline-triggers", action="store_true",
                   help="ignore changes to the pipeline itself (used by apply)")
    p.add_argument("--github-output")
    args = p.parse_args(argv)

    stacks = discover_stacks(args.stack_dirs, args.stack_root, Path.cwd())
    triggers = (".tflint.hcl",) if args.no_pipeline_triggers else DEFAULT_ALL_STACK_TRIGGERS

    if args.base == NULL_SHA:
        selected = stacks  # new branch: nothing to diff against, check everything
    else:
        selected = stacks_for_changes(changed_files(args.base, args.head, args.mode == "pr"),
                                      stacks, args.modules_dir, triggers)

    matrix = [{"dir": s, "slug": slug(s)} for s in selected]
    print(f"stacks: {', '.join(selected) or '(none)'}", file=sys.stderr)
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as f:
            f.write(f"stacks={json.dumps(matrix)}\n")
            f.write(f"any={'true' if matrix else 'false'}\n")
    else:
        print(json.dumps(matrix))
    return 0


if __name__ == "__main__":
    sys.exit(main())

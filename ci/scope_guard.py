#!/usr/bin/env python3
"""Plan scope guard (section 7, step 7).

Agent PRs must touch only what their proposal manifest declared:
  - every changed address must be listed for this stack, and
  - its action (create/update/replace/delete) must be one the manifest allows.
So an undeclared delete or replace always fails.

An agent PR is one whose branch starts with "optimizer/" OR that adds/changes a
manifest under .optimizer/manifests/. If only one of the two is true, the PR is
still treated as an agent PR and must satisfy both (a missing manifest fails).

Human PRs just get a summary of creates/updates/replaces/deletes.

Manifest format (one file per proposal, .optimizer/manifests/<proposal_id>.json):
  {
    "version": 1,
    "proposal_id": "cost-20261125-001",
    "changes": [
      {"stack": "sandbox", "address": "aws_instance.idle", "actions": ["update"]}
    ]
  }

Writes a markdown result to --out (for the PR comment). Exit code 1 = blocked.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

AGENT_BRANCH_PREFIX = "optimizer/"
MANIFEST_DIR = ".optimizer/manifests/"
VALID_ACTIONS = {"create", "update", "replace", "delete"}


class ManifestError(ValueError):
    pass


@dataclass
class GuardResult:
    ok: bool
    mode: str  # "agent" or "human"
    lines: list[str] = field(default_factory=list)

    def markdown(self, stack: str) -> str:
        icon = "✅" if self.ok else "❌"
        head = f"{icon} **Scope guard** (`{stack}`, {self.mode} PR)"
        return "\n".join([head, "", *self.lines]) + "\n"


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ManifestError(f"{path}: unreadable manifest ({e})") from e
    if data.get("version") != 1:
        raise ManifestError(f"{path}: version must be 1")
    if not isinstance(data.get("proposal_id"), str) or not data["proposal_id"]:
        raise ManifestError(f"{path}: proposal_id is required")
    changes = data.get("changes")
    if not isinstance(changes, list):
        raise ManifestError(f"{path}: changes must be a list")
    for c in changes:
        if not isinstance(c, dict) or not {"stack", "address", "actions"} <= c.keys():
            raise ManifestError(f"{path}: each change needs stack, address, actions")
        bad = set(c["actions"]) - VALID_ACTIONS
        if bad or not c["actions"]:
            raise ManifestError(f"{path}: invalid actions {sorted(bad) or '[]'} for {c['address']}")
    return data


def allowed_for_stack(manifests: list[dict], stack: str) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for m in manifests:
        for c in m["changes"]:
            if c["stack"].strip("/") == stack:
                allowed.setdefault(c["address"], set()).update(c["actions"])
    return allowed


def check_agent(summary: dict, allowed: dict[str, set[str]]) -> GuardResult:
    violations, ok_lines = [], []
    seen = set()
    for c in summary["changes"]:
        addr, action = c["address"], c["action"]
        seen.add(addr)
        if addr not in allowed:
            violations.append(f"- `{addr}`: **{action}** is not in the proposal manifest")
        elif action not in allowed[addr]:
            violations.append(f"- `{addr}`: plan wants **{action}**, manifest only allows "
                              f"{', '.join(sorted(allowed[addr]))}")
        else:
            ok_lines.append(f"- `{addr}`: {action} (declared)")
    missing = sorted(set(allowed) - seen)

    lines = []
    if violations:
        lines += ["Changes outside the declared scope (blocked):", *violations, ""]
    if ok_lines:
        lines += ["Declared changes:", *ok_lines, ""]
    if missing:
        # Not a failure: a declared change that doesn't show up is suspicious but harmless.
        lines += ["⚠️ Declared in the manifest but not in the plan:",
                  *[f"- `{a}`" for a in missing], ""]
    if not summary["changes"]:
        lines.append("No resource changes in this stack.")
    return GuardResult(ok=not violations, mode="agent", lines=lines)


def check_human(summary: dict) -> GuardResult:
    counts = summary["counts"]
    lines = [", ".join(f"{counts[a]} to {a}" for a in ("create", "update", "replace", "delete"))]
    risky = [c for c in summary["changes"] if c["action"] in ("replace", "delete")]
    if risky:
        lines += ["", "⚠️ Destructive changes, review carefully:",
                  *[f"- `{c['address']}`: **{c['action']}**" for c in risky]]
    return GuardResult(ok=True, mode="human", lines=lines)


def run_guard(summary: dict, stack: str, head_ref: str, manifest_paths: list[Path]) -> GuardResult:
    is_agent_branch = head_ref.startswith(AGENT_BRANCH_PREFIX)
    if not is_agent_branch and not manifest_paths:
        return check_human(summary)

    if not manifest_paths:
        return GuardResult(False, "agent", [
            f"Branch `{head_ref}` looks like an agent PR but no manifest was added under "
            f"`{MANIFEST_DIR}`. Agent PRs must declare their changes."])
    try:
        manifests = [load_manifest(p) for p in manifest_paths]
    except ManifestError as e:
        return GuardResult(False, "agent", [f"Invalid manifest: {e}"])
    return check_agent(summary, allowed_for_stack(manifests, stack))


def changed_manifests(base: str, head: str, three_dot: bool) -> list[Path]:
    rng = f"{base}...{head}" if three_dot else f"{base}..{head}"
    out = subprocess.run(["git", "diff", "--name-only", "--diff-filter=AM", rng],
                         check=True, capture_output=True, text=True).stdout
    return [Path(p) for p in out.splitlines()
            if p.startswith(MANIFEST_DIR) and p.endswith(".json")]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Terraform plan scope guard")
    p.add_argument("--summary", required=True, type=Path, help="output of plan_summary.py")
    p.add_argument("--stack", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--mode", choices=["pr", "push"], default="pr")
    p.add_argument("--head-ref", default="", help="PR branch name (empty on push)")
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)

    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    manifests = changed_manifests(args.base, args.head, args.mode == "pr")
    result = run_guard(summary, args.stack.strip("/"), args.head_ref, manifests)

    text = result.markdown(args.stack)
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())

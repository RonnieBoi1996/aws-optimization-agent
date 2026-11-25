#!/usr/bin/env python3
"""Reduce `terraform show -json tfplan` to addresses + actions only.

The full plan JSON contains attribute values (and possibly secrets), so it
never leaves the runner. This summary has no values, so it's safe to pass to
other jobs as an artifact, even in a public repo.

  terraform show -json tfplan | python3 plan_summary.py > plan-summary.json
"""

from __future__ import annotations

import json
import sys

ACTION_ORDER = ("create", "update", "replace", "delete")


def normalize(actions: list[str]) -> str | None:
    """Map Terraform's action lists to one word. None = nothing changes."""
    a = list(actions)
    if a in (["no-op"], ["read"]):
        return None
    if sorted(a) == ["create", "delete"]:  # either order: create-before-destroy or not
        return "replace"
    if len(a) == 1 and a[0] in ("create", "update", "delete"):
        return a[0]
    raise ValueError(f"unexpected action list: {actions}")


def summarize(plan: dict) -> dict:
    changes = []
    for rc in plan.get("resource_changes", []):
        action = normalize(rc["change"]["actions"])
        if action is None:
            continue
        changes.append({"address": rc["address"], "action": action, "mode": rc.get("mode", "managed")})
    changes.sort(key=lambda c: c["address"])
    counts = {a: sum(1 for c in changes if c["action"] == a) for a in ACTION_ORDER}
    return {"format_version": 1, "changes": changes, "counts": counts}


def main() -> int:
    print(json.dumps(summarize(json.load(sys.stdin)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

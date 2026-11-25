#!/usr/bin/env python3
"""Post Terraform results to pull requests (section 7, step 8).

  plan   one comment per PR with every step's result per stack plus the plan
         text. Re-runs UPDATE that comment instead of adding new ones.
  apply  after a merge, post the apply result to the PR that was merged.

Uses the `gh` CLI (preinstalled on GitHub runners) with GH_TOKEN. Stdlib only.
Plan/apply text must already be masked (account ID etc.) before it gets here.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

MARKER = "<!-- terraform-pr-comment -->"
STEPS = ("fmt", "validate", "tflint", "checkov", "plan", "scope-guard")
ICONS = {"success": "✅", "failure": "❌", "cancelled": "🚫", "skipped": "⏭️",
         "pending": "⏳", "missing": "➖"}
MAX_BODY = 60_000  # GitHub's hard limit is 65,536 characters


# ----------------------------------------------------------------- formatting

def fence_safe(text: str) -> str:
    """Stop text from closing our ``` code fence early."""
    return text.replace("```", "`​``")


def truncate(text: str, limit: int, keep_tail: int = 1500) -> str:
    """Cut the middle out of long output. Keeps the start (first changes) and the
    end (Terraform's "Plan: N to add..." / "Apply complete!" summary)."""
    if len(text) <= limit:
        return text
    keep_tail = min(keep_tail, limit // 3)
    head = text[: limit - keep_tail]
    tail = text[-keep_tail:]
    cut = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{cut:,} characters truncated; full output in the workflow log] ...\n\n{tail}"


def step_results(jobs: list[dict], stack_dir: str) -> dict[str, str]:
    """Map step name -> result for one stack, from the Actions jobs API."""
    by_name = {j["name"]: j for j in jobs}
    out = {}
    for step in STEPS:
        job = by_name.get(f"{step} ({stack_dir})")
        if job is None:
            out[step] = "missing"
        elif job.get("status") != "completed":
            out[step] = "pending"
        else:
            out[step] = job.get("conclusion") or "pending"
    return out


def build_plan_comment(stacks: list[str], jobs: list[dict], plans: dict[str, str],
                       guards: dict[str, str], run_url: str, sha: str) -> str:
    header = "| Stack | " + " | ".join(STEPS) + " |"
    rows = [header, "|" + " --- |" * (len(STEPS) + 1)]
    for s in stacks:
        res = step_results(jobs, s)
        rows.append(f"| `{s}` | " + " | ".join(ICONS.get(res[st], res[st]) for st in STEPS) + " |")

    intro = [MARKER, "## Terraform", "", *rows, ""]
    footer = ["", f"[Workflow run]({run_url}) · commit `{sha[:7]}` · "
                  "✅ passed ❌ failed ⏭️ skipped ➖ not run"]

    fixed = "\n".join(intro + footer)
    sections_budget = MAX_BODY - len(fixed) - 200 * max(len(stacks), 1)
    per_stack = max(sections_budget // max(len(stacks), 1), 2000)

    sections = []
    for s in stacks:
        guard = guards.get(s, "_Scope guard did not run._\n")
        plan = plans.get(s)
        plan_budget = max(per_stack - len(guard) - 200, 1000)
        block = [f"### `{s}`", "", guard]
        if plan is None:
            block.append("_No plan output (plan did not run)._")
        else:
            block += ["<details><summary>Plan output</summary>", "", "```",
                      fence_safe(truncate(plan.strip(), plan_budget)), "```", "</details>"]
        sections.append("\n".join(block))

    body = "\n".join(intro + sections + footer)
    return body[:MAX_BODY]  # final safety net; budgets above should keep us under


def build_apply_comment(stack: str, result: str, log: str, run_url: str, sha: str) -> str:
    icon = ICONS.get(result, result)
    return "\n".join([
        f"## {icon} Terraform apply: `{stack}` ({result})", "",
        "<details><summary>Apply output</summary>", "", "```",
        fence_safe(truncate(log.strip(), MAX_BODY - 2000)), "```", "</details>", "",
        f"[Workflow run]({run_url}) · commit `{sha[:7]}`",
    ])


# ----------------------------------------------------------------- GitHub API

def gh_api(path: str, method: str = "GET", body: dict | None = None, paginate: bool = False):
    cmd = ["gh", "api", path, "-X", method]
    if paginate:
        cmd.append("--paginate")
    if body is not None:
        cmd += ["--input", "-"]
    out = subprocess.run(cmd, input=json.dumps(body) if body is not None else None,
                         check=True, capture_output=True, text=True).stdout
    if not out.strip():
        return None
    if paginate:
        # --paginate concatenates JSON documents; decode them one by one
        dec, items, idx = json.JSONDecoder(), [], 0
        while idx < len(out):
            obj, idx = dec.raw_decode(out, idx)
            items.append(obj)
            while idx < len(out) and out[idx].isspace():
                idx += 1
        return items
    return json.loads(out)


def upsert_comment(repo: str, pr: int, body: str) -> None:
    pages = gh_api(f"repos/{repo}/issues/{pr}/comments?per_page=100", paginate=True) or []
    for page in pages:
        for c in page:
            if MARKER in c.get("body", "") and c.get("user", {}).get("login") == "github-actions[bot]":
                gh_api(f"repos/{repo}/issues/comments/{c['id']}", "PATCH", {"body": body})
                print(f"updated comment {c['id']}")
                return
    gh_api(f"repos/{repo}/issues/{pr}/comments", "POST", {"body": body})
    print("created comment")


# ----------------------------------------------------------------- commands

def cmd_plan(args) -> int:
    stacks = [s["dir"] for s in json.loads(args.stacks)]
    pages = gh_api(f"repos/{args.repo}/actions/runs/{args.run_id}/jobs?per_page=100", paginate=True) or []
    jobs = [j for page in pages for j in page.get("jobs", [])]
    art = Path(args.artifacts_dir)
    plans, guards = {}, {}
    for s in json.loads(args.stacks):
        plan_file = art / f"plan-{s['slug']}" / "plan.txt"
        guard_file = art / f"guard-{s['slug']}" / "guard.md"
        if plan_file.exists():
            plans[s["dir"]] = plan_file.read_text(encoding="utf-8", errors="replace")
        if guard_file.exists():
            guards[s["dir"]] = guard_file.read_text(encoding="utf-8", errors="replace")
    body = build_plan_comment(stacks, jobs, plans, guards, args.run_url, args.sha)
    upsert_comment(args.repo, args.pr, body)
    return 0


def cmd_apply(args) -> int:
    prs = gh_api(f"repos/{args.repo}/commits/{args.sha}/pulls") or []
    if not prs:
        print("no PR associated with this commit; nothing to comment on")
        return 0
    log = Path(args.log).read_text(encoding="utf-8", errors="replace") if Path(args.log).exists() else "(no output)"
    body = build_apply_comment(args.stack, args.result, log, args.run_url, args.sha)
    gh_api(f"repos/{args.repo}/issues/{prs[0]['number']}/comments", "POST", {"body": body})
    print(f"commented on PR #{prs[0]['number']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("plan")
    pp.add_argument("--repo", required=True)
    pp.add_argument("--pr", required=True, type=int)
    pp.add_argument("--run-id", required=True)
    pp.add_argument("--run-url", required=True)
    pp.add_argument("--sha", required=True)
    pp.add_argument("--stacks", required=True, help="JSON from detect_stacks.py")
    pp.add_argument("--artifacts-dir", required=True)
    pp.set_defaults(func=cmd_plan)

    pa = sub.add_parser("apply")
    pa.add_argument("--repo", required=True)
    pa.add_argument("--sha", required=True)
    pa.add_argument("--stack", required=True)
    pa.add_argument("--result", required=True)
    pa.add_argument("--log", required=True)
    pa.add_argument("--run-url", required=True)
    pa.set_defaults(func=cmd_apply)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

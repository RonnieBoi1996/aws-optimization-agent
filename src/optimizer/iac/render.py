"""Manifest + PR description for a synthesized change. Built by code from
validated fields; the only model-written text is the title and the short
"how the edit works" note, both scrubbed and length-limited."""

from __future__ import annotations

from optimizer.scrub import scrub

MANIFEST_DIR = ".optimizer/manifests"  # must match ci/scope_guard.py


def manifest(proposal_id: str, stack: str, plan_changes: list[dict]) -> dict:
    return {"version": 1, "proposal_id": proposal_id,
            "changes": [{"stack": stack, "address": c["address"], "actions": sorted(c["actions"])}
                        for c in plan_changes]}


def manifest_path(proposal_id: str) -> str:
    return f"{MANIFEST_DIR}/{proposal_id}.json"


def branch_name(proposal_id: str) -> str:
    return f"optimizer/{proposal_id}"  # the scope guard's agent-PR prefix


def _money(v: float) -> str:
    return f"saves ${abs(v):,.2f}/month" if v < 0 else f"costs ${v:,.2f}/month more"


def mask_account(text: str, account_id: str) -> str:
    """The target repos are public: the account ID never goes to GitHub (same as the CI comments)."""
    return text.replace(account_id, "<account>") if account_id else text


def pr_title(model_title: str, proposal: dict, account_id: str) -> str:
    title = " ".join(scrub(model_title or proposal["change_summary"]).split())
    return mask_account(("[optimizer] " + title)[:90], account_id)


def pr_body(proposal: dict, run_id: str, stack: str, repo: str, plan_changes: list[dict],
            explanation: str, diff: str, account_id: str) -> str:
    p, impact = proposal, proposal["estimated_impact"]
    ev_rows = [f"| {e['metric']} | {e['value']} {e.get('unit') or ''} | "
               f"{str(e['window_start'])[:16]} → {str(e['window_end'])[:16]} | `{e['source_tool']}` |"
               for e in p["evidence"]]
    scope = [f"- `{c['address']}`: {', '.join(sorted(c['actions']))}" for c in plan_changes]
    lines = [
        "Opened by the AWS optimizer (Cost agent). **A human must review and merge this.** "
        "The pipeline below checks that the plan touches only what's listed under *Plan scope*.",
        "",
        "## Change", "", p["change_summary"], "",
        "## Why", "", p["rationale"], "",
        "## What you give up", "", p["what_you_give_up"], "",
        "## Evidence", "", "| Metric | Value | Window | Source |", "| --- | --- | --- | --- |", *ev_rows, "",
        "## Impact and risk", "",
        f"- **Cost:** {_money(impact['monthly_cost_delta_usd'])} (estimate)",
        f"- **Risk:** {p['risk_level']} · **Reversible:** {'yes' if p['reversible'] else 'no'}"
        + (" · **Snapshot first:** yes" if p["requires_snapshot"] else ""),
        f"- **Affects availability:** {'yes' if p['affects_availability'] else 'no'}: {p['affects_availability_reason']}",
        f"- **Weakens security:** {'yes' if p['weakens_security'] else 'no'}: {p['weakens_security_reason']}",
        "",
        "## Plan scope (enforced by the scope guard)", "", f"Stack `{stack}`:", *scope, "",
        "## How the edit works", "", explanation, "",
        "<details><summary>Diff</summary>", "", "```diff", diff.rstrip(), "```", "", "</details>", "",
        "---",
        f"Proposal `{p['id']}` · run `{run_id}` · resource `{p['resource_arn']}` · repo `{repo}`",
    ]
    return mask_account(scrub("\n".join(lines)), account_id)

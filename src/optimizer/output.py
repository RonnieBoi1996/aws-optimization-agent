"""Shadow-mode output: out/<run_id>/ with JSON for machines and markdown for you."""

from __future__ import annotations

import json
from pathlib import Path

from optimizer.scrub import scrub


def _money(v: float) -> str:
    return f"-${abs(v):,.2f}/mo" if v < 0 else f"+${v:,.2f}/mo"


def _proposal_md(p: dict, heading: str) -> list[str]:
    impact = p["estimated_impact"]
    lines = [
        f"### {heading}: {p['change_summary']}",
        "",
        f"- **Resource:** `{p['resource_arn']}`",
        f"- **Service:** {p['service']}" + (f" · **Stack:** `{p['iac_repo']}/{p['iac_path']}`" if p.get("iac_path") else " · **unmanaged** (no IaC tags)"),
        f"- **Impact:** {_money(impact['monthly_cost_delta_usd'])} · **Risk:** {p['risk_level']} · "
        f"**Reversible:** {'yes' if p['reversible'] else 'no'}" + (" · **Snapshot first**" if p["requires_snapshot"] else ""),
        f"- **Why:** {p['rationale']}",
        f"- **What you give up:** {p['what_you_give_up']}",
        "",
        "| Evidence | Value | Window | Tool |",
        "| --- | --- | --- | --- |",
    ]
    for e in p["evidence"]:
        val = f"{e['value']} {e.get('unit') or ''}".strip()
        lines.append(f"| {e['metric']} | {val} | {e['window_start'][:10]} → {e['window_end'][:10]} | {e['source_tool']} |")
    return lines + [""]


def render_summary(run_id: str, proposals: list[dict], unmanaged: list[dict], rejected: list[dict],
                   agent_stop: str, usage_table: str, skipped: list[dict] | None = None,
                   changes: list[dict] | None = None) -> str:
    skipped, changes = skipped or [], changes or []
    total = sum(p["estimated_impact"]["monthly_cost_delta_usd"] for p in proposals)
    lines = [
        f"# Optimizer run {run_id} (shadow mode)",
        "",
        f"- **Proposals (managed):** {len(proposals)}, estimated {_money(total)}",
        f"- **Unmanaged findings:** {len(unmanaged)} (no IaC tags, never turned into PRs)",
        f"- **Rejected by validation:** {len(rejected)}",
        f"- **Looked at, not proposed:** {len(skipped)}",
        f"- **Agent finished:** {agent_stop}",
        "",
        "Nothing was changed. These are proposals only.",
        "",
    ]
    if proposals:
        lines += ["## Proposals", ""]
        for i, p in enumerate(proposals, 1):
            lines += _proposal_md(p, f"P{i}")
    if unmanaged:
        lines += ["## Unmanaged findings", ""]
        for i, p in enumerate(unmanaged, 1):
            lines += _proposal_md(p, f"U{i}")
    if rejected:
        lines += ["## Rejected", "", "| Resource | Reason |", "| --- | --- |"]
        lines += [f"| `{r.get('resource_arn') or '?'}` | {r['reason']} |" for r in rejected]
        lines.append("")
    if skipped:
        lines += ["## Looked at, not proposed (agent's reasons)", "", "| Resource | Reason |", "| --- | --- |"]
        lines += [f"| `{s['resource_arn']}` | {s['reason']} |" for s in skipped]
        lines.append("")
    if changes:
        lines += ["## Terraform changes (dry run: nothing was pushed)", "",
                  "| Proposal | Result | Stack | Plan scope / reason |", "| --- | --- | --- | --- |"]
        for c in changes:
            scope = ", ".join(f"`{x['address']}` {'/'.join(x['actions'])}" for x in c.get("plan_changes") or [])
            lines.append(f"| {c['proposal_id']} | {c['status']} | {c.get('stack', '')} | {scope or c.get('reason', '')} |")
        lines += ["", "Files: `changes/<proposal>/` (change.diff, manifest.json, pr_description.md). "
                  "To open one as a PR: `optimizer pr --run-id <run> --proposal <proposal>`.", ""]
    lines += ["## Token usage", "", "```text", usage_table, "```", ""]
    return scrub("\n".join(lines))


def write_outputs(out_dir: Path, run_id: str, proposals: list[dict], unmanaged: list[dict],
                  rejected: list[dict], agent_stop: str, usage_table: str,
                  skipped: list[dict] | None = None, changes: list[dict] | None = None) -> Path:
    folder = out_dir / run_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in (("proposals", proposals), ("unmanaged", unmanaged), ("rejected", rejected),
                       ("skipped", skipped or [])):
        (folder / f"{name}.json").write_text(scrub(json.dumps(data, indent=2, default=str)), encoding="utf-8")
    (folder / "summary.md").write_text(
        render_summary(run_id, proposals, unmanaged, rejected, agent_stop, usage_table, skipped, changes),
        encoding="utf-8")
    return folder

"""`optimizer pr`: open ONE pull request for a change the synthesizer already wrote.

You pick the proposal; until the M7 human gate exists, that choice IS the gate.
No model is involved here. Steps:
  1. Load out/<run_id>/changes/<proposal_id>/ (the synthesizer's accepted edits).
  2. Fresh clone of the base branch; re-apply the edits and re-run every check
     (paths, addresses, snapshot rule, no IAM, fmt, validate). If main moved and
     the edits no longer fit, stop: re-run the optimizer instead of guessing.
  3. Through the official GitHub MCP server, started with exactly four tools:
     list_pull_requests (don't open a duplicate), create_branch, push_files
     (the changed .tf files + the manifest, one commit), create_pull_request.
     The server process has no merge tool, and this module refuses any tool not
     on ALLOWED_TOOLS as well.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from mcp import Client, StdioServerParameters

from optimizer.config import REPO_ROOT, Config
from optimizer.iac import render, workspace
from optimizer.iac.github_app import installation_token
from optimizer.iac.paths import check_stack
from optimizer.iac.synthesizer import check_change

log = logging.getLogger(__name__)

ALLOWED_TOOLS = ("create_branch", "create_pull_request", "list_pull_requests", "push_files")


class GitHubTools:
    """The only door to GitHub. Anything not on ALLOWED_TOOLS (merge, review, delete...) is refused."""

    def __init__(self, client: Any):
        self.client = client
        self.calls: list[str] = []

    async def call(self, name: str, args: dict) -> str:
        if name not in ALLOWED_TOOLS:
            raise PermissionError(f"GitHub tool {name!r} is not allowed (rule 3: no merge rights)")
        self.calls.append(name)
        result = await self.client.call_tool(name, args)
        text = "\n".join(getattr(b, "text", "") for b in result.content if getattr(b, "type", "") == "text")
        if result.is_error:
            raise RuntimeError(f"GitHub {name} failed: {text[:500]}")
        return text


def github_mcp_params(config: Config, token: str) -> StdioServerParameters:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST", "XDG_RUNTIME_DIR") if k in os.environ}
    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token  # `-e NAME` below passes it by name, not on the command line
    return StdioServerParameters(command="docker", args=[
        "run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN", config.github.mcp_image,
        "stdio", f"--tools={','.join(ALLOWED_TOOLS)}"], env=env)


def _pr_url(text: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text.strip()[:300]
    return data.get("html_url") or data.get("url") or str(data)[:300]


def load_change(out_dir: Path, run_id: str, proposal_id: str) -> tuple[dict, dict]:
    folder = out_dir / run_id / "changes" / proposal_id
    if not (folder / "result.json").exists():
        raise RuntimeError(f"no synthesized change at {folder}; run `optimizer run` first")
    result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "ok":
        raise RuntimeError(f"{proposal_id} has no change to publish ({result['status']}: {result.get('reason')})")
    proposals = json.loads((out_dir / run_id / "proposals.json").read_text(encoding="utf-8"))
    proposal = next((p for p in proposals if p["id"] == proposal_id), None)
    if proposal is None:
        raise RuntimeError(f"{proposal_id} isn't in {run_id}/proposals.json")
    return result, proposal


async def publish(config: Config, run_id: str, proposal_id: str, *, server: Any = None,
                  token_fn: Callable[[str], str] = installation_token,
                  clone_fn: Callable[..., str] = workspace.clone,
                  plugin_cache: Path | None = None) -> dict:
    out_dir = config.resolve(config.paths.out_dir)
    result, proposal = load_change(out_dir, run_id, proposal_id)
    owner, base = config.github.owner, config.github.base_branch
    repo = result["repo"]
    stack = check_stack(repo, result["stack"], {r: t.stacks for r, t in config.github.targets.items()})

    with tempfile.TemporaryDirectory(prefix="optimizer-pr-") as tmp:
        clone = Path(tmp) / repo
        base_sha = clone_fn(owner, repo, base, clone)
        try:
            checked = check_change(clone, stack, result["edits"], result["plan_changes"],
                                   bool(proposal["requires_snapshot"]),
                                   plugin_cache or REPO_ROOT / ".cache" / "terraform-plugins")
        except ValueError as e:
            raise RuntimeError(f"the change no longer passes its checks on {base} ({base_sha[:8]}); "
                               f"re-run the optimizer.\n{e}") from None

    acct = config.aws.account_id
    branch = render.branch_name(proposal_id)
    title = render.pr_title(result["title"], proposal, acct)
    body = render.pr_body(proposal, run_id, stack, repo, checked.plan_changes, result["explanation"],
                          checked.diff, acct)
    manifest = render.manifest(proposal_id, stack, checked.plan_changes)
    files = [{"path": p, "content": c} for p, c in sorted(checked.files.items())]
    files.append({"path": render.manifest_path(proposal_id), "content": json.dumps(manifest, indent=2) + "\n"})
    message = render.mask_account(f"{title}\n\nProposal {proposal_id}, optimizer run {run_id}.", acct)

    async with Client(server or github_mcp_params(config, token_fn(repo))) as client:
        gh = GitHubTools(client)
        existing = json.loads(await gh.call("list_pull_requests", {
            "owner": owner, "repo": repo, "head": f"{owner}:{branch}", "state": "open",
            "fields": ["number", "html_url"]}) or "[]")
        if existing:
            return {"status": "exists", "url": existing[0].get("html_url"), "branch": branch, "calls": gh.calls}
        await gh.call("create_branch", {"owner": owner, "repo": repo, "branch": branch, "from_branch": base})
        await gh.call("push_files", {"owner": owner, "repo": repo, "branch": branch, "files": files,
                                     "message": message})
        pr = await gh.call("create_pull_request", {"owner": owner, "repo": repo, "title": title,
                                                   "head": branch, "base": base, "body": body})
    info = {"status": "opened", "url": _pr_url(pr), "branch": branch, "base_sha": base_sha, "calls": gh.calls}
    (out_dir / run_id / "changes" / proposal_id / "pr.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info

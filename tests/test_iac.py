"""IaC synthesizer (M5): paths, edits, checks, synthesizer loop, publisher, GitHub App auth.

Terraform runs for real (fmt + validate) on stacks that use only the built-in
`terraform_data` resource, so no provider download or network is needed.
"""

import asyncio
import json
import os
import shutil
import stat
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from optimizer.config import load_config
from optimizer.iac import checks, render
from optimizer.iac.edits import Edit, EditError, apply_edits, unified_diff
from optimizer.iac.paths import PathRefused, check_file, check_stack
from optimizer.iac.publisher import ALLOWED_TOOLS, GitHubTools, publish
from optimizer.iac.synthesizer import SUBMIT, Synthesizer, build_prefix, build_context, check_change, write_change
from optimizer.llm import LLM, build_request
from optimizer.logging_setup import RunLog
from test_cost_agent import ScriptedClient, block, msg, proposal

TARGETS = {"aws-optimization-agent": ["sandbox"], "infra-live": ["stacks/*"]}
ACCT = "123456789012"
needs_tf = pytest.mark.skipif(shutil.which("terraform") is None, reason="terraform not installed")

STACK_TF = '''\
locals {
  volumes = {
    data    = { name = "sandbox-data-1" }
    archive = { name = "sandbox-archive-vol" }
  }
}

resource "terraform_data" "vol" {
  for_each = local.volumes
  input    = each.value.name
}

resource "terraform_data" "web" {
  input = "t3.nano"
}
'''


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "clone"
    (root / "sandbox").mkdir(parents=True)
    (root / "sandbox" / "main.tf").write_text(STACK_TF)
    (root / "infra").mkdir()
    (root / "infra" / "iam.tf").write_text('resource "terraform_data" "role" {}\n')
    return root


@pytest.fixture
def config(tmp_path):
    c = load_config()
    c.paths.out_dir = str(tmp_path / "out")
    c.paths.runs_dir = str(tmp_path / "runs")
    c.terraform_mcp.enabled = False
    return c


def managed(**over):
    p = proposal(arn=f"arn:aws:ec2:eu-west-1:{ACCT}:volume/vol-0abc")
    p.update(id="cost-r1-001", agent="cost", run_id="r1", service="sandbox",
             iac_repo="aws-optimization-agent", iac_path="sandbox", expected_plan_addresses=[])
    p.update(over)
    return p


REMOVE_DATA = [{"path": "sandbox/main.tf", "old": '    data    = { name = "sandbox-data-1" }\n', "new": ""}]
DELETE_DATA = [{"address": 'terraform_data.vol["data"]', "actions": ["delete"]}]


# ---- paths (rule 4 in code) ----------------------------------------------------

@pytest.mark.parametrize("repo_name,stack,ok", [
    ("aws-optimization-agent", "sandbox", True),
    ("aws-optimization-agent", "infra", False),        # the agent's own roles
    ("aws-optimization-agent", ".github", False),
    ("infra-live", "stacks/app", True),
    ("infra-live", ".github/workflows", False),
    ("infra-live", ".optimizer/manifests", False),     # code writes manifests, not the model
    ("infra-live", "modules/vpc", False),              # not a configured stack
    ("infra-live", "stacks/../infra", False),
    ("someone-else", "stacks/app", False),
])
def test_check_stack(repo_name, stack, ok):
    if ok:
        assert check_stack(repo_name, stack, TARGETS)
    else:
        with pytest.raises(PathRefused):
            check_stack(repo_name, stack, TARGETS)


def test_own_repo_rule_beats_config():
    with pytest.raises(PathRefused, match="only sandbox"):
        check_stack("aws-optimization-agent", "infra", {"aws-optimization-agent": ["*"]})


def test_check_file(repo):
    assert check_file("sandbox/main.tf", "sandbox", repo) == "sandbox/main.tf"
    for bad in ("infra/iam.tf", "sandbox/../infra/iam.tf", "/etc/passwd", "sandbox/notes.md"):
        with pytest.raises(PathRefused):
            check_file(bad, "sandbox", repo)
    (repo / "sandbox" / "link.tf").symlink_to(repo / "infra" / "iam.tf")
    with pytest.raises(PathRefused, match="symlink"):
        check_file("sandbox/link.tf", "sandbox", repo)


# ---- edits -----------------------------------------------------------------------

def test_edits_apply_create_and_diff(repo):
    changed = apply_edits(repo, "sandbox", [
        Edit("sandbox/main.tf", 'input = "t3.nano"', 'input = "t3.micro"'),
        Edit("sandbox/new.tf", "", 'resource "terraform_data" "extra" {}\n')])
    diff = unified_diff(changed)
    assert '-  input = "t3.nano"' in diff and '+  input = "t3.micro"' in diff and "/dev/null" in diff


@pytest.mark.parametrize("edit,match", [
    (Edit("sandbox/main.tf", "not in the file", "x"), "found 0"),
    (Edit("sandbox/main.tf", "input", "x"), "found 2"),          # ambiguous
    (Edit("sandbox/main.tf", "", "x"), "already exists"),
    (Edit("sandbox/gone.tf", "a", "b"), "doesn't exist"),
])
def test_bad_edits_fail_loudly(repo, edit, match):
    with pytest.raises(EditError, match=match):
        apply_edits(repo, "sandbox", [edit])


# ---- checks --------------------------------------------------------------------------

def test_addresses_must_exist_in_code():
    one = lambda a: checks.parse_plan_changes([{"address": a, "actions": ["update"]}])
    assert checks.check_addresses(one('terraform_data.vol["data"]'), STACK_TF, STACK_TF) == []
    assert checks.check_addresses(one("terraform_data.web"), STACK_TF, STACK_TF) == []
    for addr, expected in (("aws_instance.ghost", "no resource"), ("data.aws_ami.x", "data sources"),
                           ("module.vpc.aws_vpc.this", "no module"), ("not an address", "isn't a Terraform")):
        assert expected in checks.check_addresses(one(addr), STACK_TF, STACK_TF)[0]
    two = checks.parse_plan_changes([{"address": 'terraform_data.vol["data"]', "actions": ["update"]},
                                     {"address": 'terraform_data.vol["archive"]', "actions": ["update"]}])
    assert "ONLY the proposal's resource" in checks.check_addresses(two, STACK_TF, STACK_TF)[0]
    with pytest.raises(ValueError, match="actions"):
        checks.parse_plan_changes([{"address": "a.b", "actions": ["destroy-everything"]}])


def test_no_iam_changes():
    assert checks.check_no_iam('+resource "aws_iam_role" "x" {}\n')
    assert checks.check_no_iam('--- a/x.tf\n+++ b/x.tf\n-  size = 1\n+  size = 2\n') == []


VOL_TF = '''resource "aws_ebs_volume" "a" {
  size = 1
  tags = { Name = "x" }
}
resource "aws_ebs_volume" "b" {
  size           = 1
  final_snapshot = true
}
'''


def test_snapshot_rule_needs_final_snapshot_already_in_code():
    delete = lambda n: checks.parse_plan_changes([{"address": f"aws_ebs_volume.{n}", "actions": ["delete"]}])
    assert checks.check_snapshot_rule(delete("a"), True, VOL_TF)           # not set yet -> step 1 only
    assert checks.check_snapshot_rule(delete("b"), True, VOL_TF) == []     # already set -> may delete
    assert checks.check_snapshot_rule(delete("a"), False, VOL_TF) == []    # no snapshot needed
    update = checks.parse_plan_changes([{"address": "aws_ebs_volume.a", "actions": ["update"]}])
    assert checks.check_snapshot_rule(update, True, VOL_TF) == []          # setting it is fine
    assert "tags" in checks.block_text(VOL_TF, "aws_ebs_volume", "a")      # nested braces handled


@needs_tf
def test_check_change_runs_fmt_and_validate(repo, tmp_path):
    cache = tmp_path / "plugins"
    ok = check_change(repo, "sandbox", [{"path": "sandbox/main.tf", "old": 'input = "t3.nano"',
                                         "new": 'input    =    "t3.micro"'}],
                      [{"address": "terraform_data.web", "actions": ["update"]}], False, cache)
    assert 'input = "t3.micro"' in ok.files["sandbox/main.tf"]  # fmt cleaned the spacing
    assert (repo / "sandbox" / "main.tf").read_text() == STACK_TF  # the clone itself is untouched
    with pytest.raises(ValueError, match="validate"):
        check_change(repo, "sandbox", [{"path": "sandbox/main.tf", "old": 'input = "t3.nano"',
                                        "new": "input = local.nope"}],
                     [{"address": "terraform_data.web", "actions": ["update"]}], False, cache)


# ---- manifest / PR text ------------------------------------------------------------

def test_manifest_and_branch_match_the_ci_scope_guard(tmp_path):
    import scope_guard  # ci/scope_guard.py: the manifest must be what CI expects
    m = render.manifest("cost-r1-001", "sandbox", DELETE_DATA)
    path = tmp_path / "m.json"
    path.write_text(json.dumps(m))
    assert scope_guard.load_manifest(path)["changes"][0]["address"] == 'terraform_data.vol["data"]'
    assert render.branch_name("cost-r1-001").startswith(scope_guard.AGENT_BRANCH_PREFIX)
    assert render.manifest_path("x").startswith(scope_guard.MANIFEST_DIR)


def test_pr_text_never_contains_the_account_id():
    p = managed()
    body = render.pr_body(p, "r1", "sandbox", "aws-optimization-agent", DELETE_DATA, "Removes it.", "diff", ACCT)
    title = render.pr_title(f"Delete arn:aws:ec2:eu-west-1:{ACCT}:volume/v", p, ACCT)
    assert ACCT not in body and ACCT not in title and "<account>" in body
    for heading in ("## Change", "## Why", "## What you give up", "## Evidence", "## Plan scope"):
        assert heading in body


# ---- synthesizer loop (scripted model) ------------------------------------------------

def synth_for(config, repo, responses, tmp_path):
    run_log = RunLog(tmp_path / "runs", "r1")
    fake = ScriptedClient(responses)
    s = Synthesizer(config, LLM(config, run_log=run_log, client=fake), "r1", tmp_path / "work",
                    plugin_cache=tmp_path / "plugins")
    s._clones["aws-optimization-agent"] = (repo, "abc123")  # no network: use the local fixture repo
    return s, fake


def submit(**inp):
    return msg(block("tool_use", id=f"s{len(inp)}", name=SUBMIT, input=inp))


@needs_tf
def test_synthesizer_retries_after_failed_checks_then_succeeds(config, repo, tmp_path):
    bad = dict(title="Remove data volume", explanation="x", edits=REMOVE_DATA,
               plan_changes=[{"address": "terraform_data.ghost", "actions": ["delete"]}])
    good = dict(bad, plan_changes=DELETE_DATA, explanation="Removes the data entry from local.volumes.")
    s, fake = synth_for(config, repo, [submit(**bad), submit(**good)], tmp_path)
    r = asyncio.run(s.synthesize(managed(requires_snapshot=False), {"Name": "sandbox-data-1"}))
    assert r.status == "ok" and r.plan_changes == DELETE_DATA
    assert "sandbox-data-1" not in r.files["sandbox/main.tf"]
    # The model saw why its first try failed:
    retry_msg = json.dumps(fake.requests[1]["messages"][-1]["content"], default=str)
    assert "no resource" in retry_msg and "ghost" in retry_msg

    folder = write_change(Path(config.paths.out_dir), "r1", managed(requires_snapshot=False), r, ACCT)
    assert json.loads((folder / "manifest.json").read_text())["proposal_id"] == "cost-r1-001"
    assert ACCT not in (folder / "pr_description.md").read_text()


def test_synthesizer_refuses_protected_stack_without_calling_the_model(config, repo, tmp_path):
    s, fake = synth_for(config, repo, [], tmp_path)
    r = asyncio.run(s.synthesize(managed(iac_path="infra"), {}))
    assert r.status == "refused" and "only sandbox" in r.reason and fake.requests == []


def test_synthesizer_give_up_is_recorded(config, repo, tmp_path):
    s, _ = synth_for(config, repo, [submit(title="t", explanation="e", edits=[], plan_changes=[],
                                           give_up_reason="resource is not defined in this stack")], tmp_path)
    r = asyncio.run(s.synthesize(managed(), {}))
    assert r.status == "gave_up" and "not defined" in r.reason


def test_synth_prefix_is_byte_identical_between_runs(config):
    files = {"sandbox/main.tf": STACK_TF}
    reqs = [build_request(config.models.smart, build_prefix([], build_context("r", "sandbox", files)), "task")
            for _ in range(2)]
    strip = lambda r: json.dumps({k: r[k] for k in ("tools", "system")} | {"ctx": r["messages"][0]["content"][0]},
                                 sort_keys=True)
    assert strip(reqs[0]) == strip(reqs[1])


# ---- publisher (fake GitHub MCP server) ---------------------------------------------

def fake_github() -> tuple[MCPServer, list]:
    srv, log = MCPServer("fake-github"), []

    @srv.tool()
    def list_pull_requests(owner: str, repo: str, head: str = "", state: str = "open", fields: list[str] | None = None) -> str:
        log.append(("list_pull_requests", head))
        return "[]"

    @srv.tool()
    def create_branch(owner: str, repo: str, branch: str, from_branch: str = "") -> str:
        log.append(("create_branch", branch, from_branch))
        return "{}"

    @srv.tool()
    def push_files(owner: str, repo: str, branch: str, files: list[dict], message: str) -> str:
        log.append(("push_files", branch, files, message))
        return "{}"

    @srv.tool()
    def create_pull_request(owner: str, repo: str, title: str, head: str, base: str, body: str = "") -> str:
        log.append(("create_pull_request", title, head, base, body))
        return json.dumps({"html_url": "https://github.com/o/r/pull/7"})

    @srv.tool()
    def merge_pull_request(owner: str, repo: str, pullNumber: int) -> str:  # exists on the real server too
        log.append(("merge_pull_request",))
        return "merged"

    return srv, log


@needs_tf
def test_publish_rechecks_and_opens_exactly_one_pr(config, repo, tmp_path):
    s, _ = synth_for(config, repo, [submit(title="Remove sandbox-data-1", explanation="Drops the entry.",
                                           edits=REMOVE_DATA, plan_changes=DELETE_DATA)], tmp_path)
    p = managed(requires_snapshot=False)
    r = asyncio.run(s.synthesize(p, {}))
    out = Path(config.paths.out_dir)
    write_change(out, "r1", p, r, ACCT)
    (out / "r1" / "proposals.json").write_text(json.dumps([p]))

    srv, log = fake_github()
    clone_fn = lambda owner, name, branch, dest: (shutil.copytree(repo, dest), "abc123")[1]
    info = asyncio.run(publish(config, "r1", "cost-r1-001", server=srv, token_fn=lambda r: "unused",
                               clone_fn=clone_fn, plugin_cache=tmp_path / "plugins"))

    assert info["status"] == "opened" and info["url"].endswith("/pull/7")
    assert [c[0] for c in log] == ["list_pull_requests", "create_branch", "push_files", "create_pull_request"]
    pushed = {f["path"]: f["content"] for f in log[2][2]}
    assert set(pushed) == {"sandbox/main.tf", ".optimizer/manifests/cost-r1-001.json"}
    assert log[1][1] == "optimizer/cost-r1-001" and log[3][3] == "main"
    assert all(ACCT not in json.dumps(c) for c in log)


def test_github_tools_refuse_merge():
    srv, log = fake_github()

    async def go():
        from mcp import Client
        async with Client(srv) as c:
            try:
                await GitHubTools(c).call("merge_pull_request", {"owner": "o", "repo": "r", "pullNumber": 1})
            except PermissionError as e:
                return e

    err = asyncio.run(go())
    assert isinstance(err, PermissionError) and "no merge rights" in str(err)
    assert log == [] and "merge_pull_request" not in ALLOWED_TOOLS


# ---- GitHub App auth ------------------------------------------------------------------

def test_app_jwt_and_scoped_installation_token(tmp_path, monkeypatch):
    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from optimizer.iac import github_app

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    claims = pyjwt.decode(github_app.app_jwt("42", pem, now=1_000_000), key.public_key(), algorithms=["RS256"],
                          options={"verify_exp": False, "verify_iat": False})
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "42"}

    key_file = tmp_path / "app.pem"
    key_file.write_text(pem)
    key_file.chmod(0o644)
    monkeypatch.setenv("OPTIMIZER_GITHUB_APP_ID", "42")
    monkeypatch.setenv("OPTIMIZER_GITHUB_APP_INSTALLATION_ID", "7")
    monkeypatch.setenv("OPTIMIZER_GITHUB_APP_KEY_FILE", str(key_file))
    with pytest.raises(RuntimeError, match="chmod 600"):
        github_app.installation_token("infra-live")
    key_file.chmod(0o600)

    seen = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"token": "ghs_test"}'

    def opener(req, timeout):
        seen.update(url=req.full_url, body=json.loads(req.data), auth=req.get_header("Authorization"))
        return Resp()

    assert github_app.installation_token("infra-live", opener=opener) == "ghs_test"
    assert seen["url"].endswith("/app/installations/7/access_tokens")
    assert seen["body"] == {"repositories": ["infra-live"],
                            "permissions": {"contents": "write", "pull_requests": "write"}}
    assert seen["auth"].startswith("Bearer ")

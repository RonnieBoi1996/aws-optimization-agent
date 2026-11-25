import json

import pytest

from plan_summary import normalize, summarize
from scope_guard import load_manifest, run_guard, ManifestError


def plan(*changes):
    """Build a minimal `terraform show -json` document."""
    return {"resource_changes": [
        {"address": a, "mode": "managed", "change": {"actions": acts}} for a, acts in changes]}


def manifest(tmp_path, changes, name="p1", **extra):
    data = {"version": 1, "proposal_id": name, "changes": changes, **extra}
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(data))
    return p


# ---- plan_summary ----------------------------------------------------------

@pytest.mark.parametrize("actions,expected", [
    (["no-op"], None), (["read"], None), (["create"], "create"), (["update"], "update"),
    (["delete"], "delete"), (["delete", "create"], "replace"), (["create", "delete"], "replace"),
])
def test_normalize(actions, expected):
    assert normalize(actions) == expected


def test_summary_drops_noops_and_counts():
    s = summarize(plan(("a.x", ["no-op"]), ("a.y", ["update"]), ("a.z", ["delete", "create"])))
    assert [c["address"] for c in s["changes"]] == ["a.y", "a.z"]
    assert s["counts"] == {"create": 0, "update": 1, "replace": 1, "delete": 0}


def test_summary_contains_no_attribute_values():
    p = plan(("aws_db_instance.db", ["update"]))
    p["resource_changes"][0]["change"]["after"] = {"password": "hunter2"}
    assert "hunter2" not in json.dumps(summarize(p))


# ---- human PRs -------------------------------------------------------------

def test_human_pr_passes_with_summary():
    s = summarize(plan(("aws_s3_bucket.b", ["delete"]), ("aws_iam_role.r", ["create"])))
    r = run_guard(s, "sandbox", head_ref="feature/x", manifest_paths=[])
    assert r.ok and r.mode == "human"
    text = r.markdown("sandbox")
    assert "1 to create" in text and "1 to delete" in text and "aws_s3_bucket.b" in text


# ---- agent PRs -------------------------------------------------------------

def test_agent_pr_exact_scope_passes(tmp_path):
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_instance.idle", "actions": ["update"]}])
    s = summarize(plan(("aws_instance.idle", ["update"])))
    assert run_guard(s, "sandbox", "optimizer/cost-1", [m]).ok


def test_undeclared_address_fails(tmp_path):
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_instance.idle", "actions": ["update"]}])
    s = summarize(plan(("aws_instance.idle", ["update"]), ("aws_s3_bucket.other", ["update"])))
    r = run_guard(s, "sandbox", "optimizer/cost-1", [m])
    assert not r.ok and "aws_s3_bucket.other" in r.markdown("sandbox")


def test_undeclared_delete_fails(tmp_path):
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_ebs_volume.v", "actions": ["update"]}])
    s = summarize(plan(("aws_ebs_volume.v", ["delete"])))
    r = run_guard(s, "sandbox", "optimizer/cost-1", [m])
    assert not r.ok and "delete" in r.markdown("sandbox")


def test_undeclared_replace_fails(tmp_path):
    # Changing instance_type can force a replace; "update" alone must not allow it.
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_instance.big", "actions": ["update"]}])
    s = summarize(plan(("aws_instance.big", ["delete", "create"])))
    assert not run_guard(s, "sandbox", "optimizer/cost-2", [m]).ok


def test_declared_delete_passes(tmp_path):
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_ebs_volume.v", "actions": ["delete"]}])
    s = summarize(plan(("aws_ebs_volume.v", ["delete"])))
    assert run_guard(s, "sandbox", "optimizer/cost-1", [m]).ok


def test_manifest_for_other_stack_does_not_cover_this_one(tmp_path):
    m = manifest(tmp_path, [{"stack": "stacks/network", "address": "aws_instance.idle", "actions": ["update"]}])
    s = summarize(plan(("aws_instance.idle", ["update"])))
    assert not run_guard(s, "stacks/app", "optimizer/cost-1", [m]).ok


def test_agent_branch_without_manifest_fails():
    s = summarize(plan(("aws_instance.idle", ["update"])))
    r = run_guard(s, "sandbox", "optimizer/cost-1", [])
    assert not r.ok and "no manifest" in r.markdown("sandbox")


def test_manifest_without_agent_branch_is_still_enforced(tmp_path):
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_instance.idle", "actions": ["update"]}])
    s = summarize(plan(("aws_s3_bucket.sneaky", ["delete"])))
    r = run_guard(s, "sandbox", "some-human-branch", [m])
    assert not r.ok and r.mode == "agent"


def test_declared_but_missing_from_plan_warns_only(tmp_path):
    m = manifest(tmp_path, [{"stack": "sandbox", "address": "aws_instance.idle", "actions": ["update"]}])
    r = run_guard(summarize(plan()), "sandbox", "optimizer/cost-1", [m])
    assert r.ok and "not in the plan" in r.markdown("sandbox")


@pytest.mark.parametrize("bad", [
    {"version": 2, "proposal_id": "x", "changes": []},
    {"version": 1, "changes": []},
    {"version": 1, "proposal_id": "x", "changes": [{"stack": "s", "address": "a", "actions": ["destroy"]}]},
    {"version": 1, "proposal_id": "x", "changes": [{"stack": "s", "address": "a", "actions": []}]},
])
def test_invalid_manifests_rejected(tmp_path, bad):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ManifestError):
        load_manifest(p)
    assert not run_guard(summarize(plan()), "s", "optimizer/x", [p]).ok

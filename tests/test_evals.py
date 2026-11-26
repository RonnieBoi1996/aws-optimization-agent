"""Eval scorer (M4) and tag-based do_not_touch."""

from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from optimizer.evals import Case, SandboxInventory, Seeded, find_seeded, load_cases, run_time_from_id, score
from optimizer.protection import protection_reason

A = "123456789012"
RUN_ID = "20261201T120000Z-abc123"
RUN_TIME = datetime(2026, 12, 1, 12, tzinfo=timezone.utc)
ARN = {k: f"arn:aws:ec2:eu-west-1:{A}:{k}" for k in
       ("instance/i-idle", "instance/i-big", "instance/i-pm", "volume/vol-data", "volume/vol-keep")}
VPC = f"arn:aws:ec2:eu-west-1:{A}:vpc/vpc-1"

CASES = [
    Case(id="idle-ec2", agent="cost", expect="proposal", min_age_hours=24),
    Case(id="oversized-ec2", agent="cost", expect="proposal", min_age_hours=48),
    Case(id="unattached-ebs", agent="cost", expect="proposal"),
    Case(id="do-not-touch-ebs", agent="cost", expect="no_proposal"),
    Case(id="postmortem-ec2", agent="cost", expect="no_proposal"),
]


def inventory(age_hours=72):
    created = RUN_TIME - timedelta(hours=age_hours)
    by_case = {"idle-ec2": ARN["instance/i-idle"], "oversized-ec2": ARN["instance/i-big"],
               "postmortem-ec2": ARN["instance/i-pm"], "unattached-ebs": ARN["volume/vol-data"],
               "do-not-touch-ebs": ARN["volume/vol-keep"]}
    return SandboxInventory(by_case={c: Seeded(a, created) for c, a in by_case.items()},
                            sandbox_arns=set(by_case.values()) | {VPC})


def p(arn):
    return {"resource_arn": arn}


def outcomes(result):
    return {r.case: r.outcome for r in result.results}


# ---- tag-based do_not_touch -----------------------------------------------

def test_do_not_touch_by_tag_is_exact():
    rules = ["tag:Name=sandbox-archive-vol"]
    assert protection_reason("arn:x:y:z:a:volume/v", rules, {"Name": "sandbox-archive-vol"})
    assert protection_reason("arn:x:y:z:a:volume/v", rules, {"Name": "sandbox-archive-vol-2"}) is None
    assert protection_reason("arn:x:y:z:a:volume/v", rules, {}) is None
    assert protection_reason("arn:x:y:z:a:volume/v", rules, None) is None
    # A malformed entry (no "=") protects nothing rather than everything.
    assert protection_reason("arn:x:y:z:a:volume/v", ["tag:Name"], {"Name": "x"}) is None


# ---- scoring -----------------------------------------------------------------

def test_perfect_run():
    r = score(CASES, inventory(), RUN_ID,
              [p(ARN["instance/i-idle"]), p(ARN["instance/i-big"]), p(ARN["volume/vol-data"])], [], [])
    assert (r.tp, r.fp, r.fn, r.precision, r.recall, r.passed) == (3, 0, 0, 1.0, 1.0, True)
    assert outcomes(r)["do-not-touch-ebs"] == "correctly_skipped"


def test_false_positives_and_misses():
    r = score(CASES, inventory(), RUN_ID,
              [p(ARN["instance/i-idle"]), p(ARN["instance/i-pm"])], [],
              [{"resource_arn": ARN["volume/vol-data"], "reason": "resource not found in AWS"}])
    o = outcomes(r)
    assert o["postmortem-ec2"] == "false_positive"
    assert o["oversized-ec2"] == "miss"
    assert o["unattached-ebs"] == "miss"
    assert "rejected: resource not found" in next(x.detail for x in r.results if x.case == "unattached-ebs")
    assert (r.tp, r.fp, r.fn) == (1, 1, 2)
    assert r.precision == 0.5 and r.recall == pytest.approx(1 / 3) and not r.passed


def test_validator_catch_is_not_a_false_positive():
    r = score(CASES, inventory(), RUN_ID, [], [],
              [{"resource_arn": ARN["volume/vol-keep"], "reason": "protected (R1): listed in do_not_touch"}])
    assert outcomes(r)["do-not-touch-ebs"] == "caught_by_validator"
    assert r.fp == 0


def test_young_resources_are_not_ready_not_missed():
    r = score(CASES, inventory(age_hours=30), RUN_ID, [p(ARN["volume/vol-data"])], [], [])
    o = outcomes(r)
    assert o["idle-ec2"] == "miss"            # 30h >= 24h: old enough, so not proposing it is a miss
    assert o["oversized-ec2"] == "not_ready"                  # 30h < 48h
    # ...but a proposal on a young resource still counts as a hit.
    r2 = score(CASES, inventory(age_hours=1), RUN_ID, [p(ARN["instance/i-big"])], [], [])
    assert outcomes(r2)["oversized-ec2"] == "hit"


def test_unseeded_sandbox_resource_is_fp_and_outside_is_unscored():
    elsewhere = f"arn:aws:ec2:eu-west-1:{A}:instance/i-prod"
    r = score(CASES, inventory(), RUN_ID, [], [p(VPC), p(elsewhere)], [])
    assert [u.arn for u in r.unexpected] == [VPC] and r.fp == 1
    assert r.unscored == [elsewhere]


def test_missing_fixture_is_not_seeded_and_no_division_by_zero():
    r = score(CASES, SandboxInventory(by_case={}, sandbox_arns=set()), RUN_ID, [], [], [])
    assert set(outcomes(r).values()) == {"not_seeded"}
    assert r.precision is None and r.recall is None and r.passed


def test_cases_file_is_valid():
    ids = {c.id for c in load_cases()}
    assert ids == {"idle-ec2", "unattached-ebs", "oversized-ec2", "do-not-touch-ebs", "postmortem-ec2"}


def test_run_time_from_id():
    assert run_time_from_id(RUN_ID) == RUN_TIME


# ---- finding fixtures in AWS -------------------------------------------------

@mock_aws
def test_find_seeded_uses_tags_and_skips_terminated():
    ec2 = boto3.client("ec2", region_name="eu-west-1")
    ami = ec2.describe_images()["Images"][0]["ImageId"]

    def run(case):
        tags = [{"Key": "environment", "Value": "sandbox"}, {"Key": "eval_case", "Value": case}]
        return ec2.run_instances(ImageId=ami, InstanceType="t3.nano", MinCount=1, MaxCount=1,
                                 TagSpecifications=[{"ResourceType": "instance", "Tags": tags}]
                                 )["Instances"][0]["InstanceId"]

    live, dead = run("idle-ec2"), run("oversized-ec2")
    ec2.terminate_instances(InstanceIds=[dead])
    vol = ec2.create_volume(AvailabilityZone="eu-west-1a", Size=1, TagSpecifications=[{
        "ResourceType": "volume", "Tags": [{"Key": "environment", "Value": "sandbox"},
                                           {"Key": "eval_case", "Value": "unattached-ebs"}]}])["VolumeId"]
    ec2.create_volume(AvailabilityZone="eu-west-1a", Size=1)  # untagged: not part of the sandbox

    inv = find_seeded(boto3.client("resourcegroupstaggingapi", region_name="eu-west-1"), ec2)
    assert set(inv.by_case) == {"idle-ec2", "unattached-ebs"}
    assert inv.by_case["idle-ec2"].arn.endswith(f"instance/{live}")
    assert inv.by_case["unattached-ebs"].arn.endswith(f"volume/{vol}")
    assert all(s.created is not None for s in inv.by_case.values())


def test_repo_context_loads_with_fixture_services():
    """The committed context/ must parse; a typo there would only show up in a live run."""
    from optimizer.config import REPO_ROOT
    from optimizer.context import load_context
    ctx = load_context(REPO_ROOT / "context", {ARN["instance/i-pm"]: {"service": "sandbox-reports"},
                                              ARN["volume/vol-keep"]: {"service": "sandbox", "Name": "sandbox-archive-vol"}})
    assert set(ctx.services) == {"_default", "sandbox", "sandbox-reports"}
    assert "Do not downsize `sandbox-report-builder`" in ctx.context_text
    keep = ARN["volume/vol-keep"]
    assert protection_reason(keep, ctx.constraints_for(keep).do_not_touch, ctx.tag_map[keep])

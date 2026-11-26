"""aws-insights cost tools. AWS is mocked with moto; services moto lacks use small fakes."""

import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from optimizer.aws_session import WrongIdentity, check_identity
from optimizer.insights import cost_tools, pricing
from optimizer.insights.cache import cached
from optimizer.insights.cost_tools import Clients, classify_idle, summarize_cost, summarize_ec2_recommendation

REGION = "eu-west-1"
ACCOUNT = "123456789012"


class FakePricing:
    """Pricing API stand-in: returns one product with the given USD price."""
    def __init__(self, usd: str | None):
        self.usd, self.calls = usd, 0

    def get_products(self, **kw):
        self.calls += 1
        if self.usd is None:
            return {"PriceList": []}
        product = {"terms": {"OnDemand": {"t": {"priceDimensions": {"d": {"pricePerUnit": {"USD": self.usd}}}}}}}
        return {"PriceList": [json.dumps(product)]}


def make_clients(pricing_client=None, **overrides) -> Clients:
    base = dict(region=REGION, ec2=boto3.client("ec2", region_name=REGION),
                cloudwatch=boto3.client("cloudwatch", region_name=REGION),
                s3=boto3.client("s3", region_name=REGION), cost_explorer=None, compute_optimizer=None,
                pricing=pricing_client or FakePricing("0.088"),
                cloudwatch_for_region=lambda r: boto3.client("cloudwatch", region_name=r))
    base.update(overrides)
    return Clients(**base)


# ---- identity guard --------------------------------------------------------

def test_identity_guard_accepts_only_agent_role():
    check_identity(f"arn:aws:sts::{ACCOUNT}:assumed-role/optimizer-agent-read/s1", ACCOUNT, ACCOUNT)
    with pytest.raises(WrongIdentity, match="may only run as"):
        check_identity(f"arn:aws:sts::{ACCOUNT}:assumed-role/AWSReservedSSO_AdministratorAccess_x/me",
                       ACCOUNT, ACCOUNT)
    with pytest.raises(WrongIdentity, match="account"):
        check_identity("arn:aws:sts::999999999999:assumed-role/optimizer-agent-read/s1",
                       "999999999999", ACCOUNT)


# ---- idle EC2 --------------------------------------------------------------

@pytest.mark.parametrize("avg,peak,net,idle", [
    (1.0, 5.0, 0.5, True),
    (4.99, 19.9, 4.9, True),
    (5.0, 10.0, 1.0, False),     # avg at the threshold is not idle
    (1.0, 25.0, 1.0, False),     # a busy hour means it's used
    (1.0, 5.0, 50.0, False),     # network traffic means it's used
    (None, 5.0, 1.0, False),     # missing data is never "idle"
])
def test_classify_idle(avg, peak, net, idle):
    assert classify_idle(avg, peak, net) is idle


@mock_aws
def test_idle_ec2_without_metrics_reports_nothing():
    ec2 = boto3.client("ec2", region_name=REGION)
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    ec2.run_instances(ImageId=ami, InstanceType="t3.micro", MinCount=1, MaxCount=1)
    out = cost_tools.get_idle_ec2(make_clients(), days=14)
    assert out["running_instances_checked"] == 1
    assert out["idle_instances"] == []  # no CloudWatch data -> not provably idle
    assert out["window"]["days"] == 14.0 and "data_timestamp" in out


# ---- unattached EBS --------------------------------------------------------

@mock_aws
def test_unattached_ebs_found_with_cost_and_tags():
    ec2 = boto3.client("ec2", region_name=REGION)
    vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=10, VolumeType="gp3",
                            TagSpecifications=[{"ResourceType": "volume", "Tags": [
                                {"Key": "service", "Value": "sandbox"}, {"Key": "secret_note", "Value": "x"}]}])
    out = cost_tools.get_unattached_ebs(make_clients(), ACCOUNT)
    [v] = out["unattached_volumes"]
    assert v["volume_id"] == vol["VolumeId"]
    assert v["arn"] == f"arn:aws:ec2:{REGION}:{ACCOUNT}:volume/{vol['VolumeId']}"
    assert v["est_monthly_cost_usd"] == pytest.approx(0.88)
    assert v["tags"] == {"service": "sandbox"}  # only the IaC/service tags are passed on


@mock_aws
def test_unknown_price_is_none_not_guessed():
    boto3.client("ec2", region_name=REGION).create_volume(AvailabilityZone=f"{REGION}a", Size=5, VolumeType="gp3")
    out = cost_tools.get_unattached_ebs(make_clients(FakePricing(None)), ACCOUNT)
    assert out["unattached_volumes"][0]["est_monthly_cost_usd"] is None


def test_price_lookups_are_cached():
    fake = FakePricing("0.0114")
    assert pricing.ec2_hourly_usd(fake, REGION, "t3.micro") == 0.0114
    assert pricing.ec2_hourly_usd(fake, REGION, "t3.micro") == 0.0114
    assert fake.calls == 1


# ---- S3 storage-class candidates -------------------------------------------

@mock_aws
def test_bucket_with_transition_rule_is_not_a_candidate():
    s3 = boto3.client("s3", region_name=REGION)
    cfg = {"LocationConstraint": REGION}
    s3.create_bucket(Bucket="with-rule", CreateBucketConfiguration=cfg)
    s3.put_bucket_lifecycle_configuration(Bucket="with-rule", LifecycleConfiguration={"Rules": [{
        "ID": "ia", "Status": "Enabled", "Filter": {}, "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}]}]})
    s3.create_bucket(Bucket="no-rule", CreateBucketConfiguration=cfg)
    out = cost_tools.get_storage_class_candidates(make_clients())
    assert out["buckets_checked"] == 2
    facts = {b: b for b in out["not_candidates"]}
    assert "with-rule" in facts and "no-rule" in facts  # no-rule has 0 GB -> not a candidate either
    assert out["candidates"] == []


# ---- Compute Optimizer / Cost Explorer summarizers -------------------------

def test_rightsizing_summary_picks_rank_1_and_usd_savings():
    rec = {"instanceArn": "arn:aws:ec2:eu-west-1:1:instance/i-1", "finding": "Overprovisioned",
           "currentInstanceType": "m5.large", "lookBackPeriodInDays": 14,
           "utilizationMetrics": [{"name": "CPU", "statistic": "MAXIMUM", "value": 7.5}],
           "recommendationOptions": [
               {"rank": 2, "instanceType": "t3.medium"},
               {"rank": 1, "instanceType": "t3.small", "performanceRisk": 1.0,
                "savingsOpportunity": {"estimatedMonthlySavings": {"currency": "USD", "value": 51.2}}}]}
    s = summarize_ec2_recommendation(rec)
    assert (s["recommended_type"], s["est_monthly_savings_usd"], s["max_utilization"]) == ("t3.small", 51.2, {"CPU": 7.5})


def test_cost_summary_totals_and_sorts():
    day = lambda a, b: {"Groups": [{"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": a}}},
                                   {"Keys": ["S3"], "Metrics": {"UnblendedCost": {"Amount": b}}}]}
    services, total = summarize_cost([day("1.5", "0.25"), day("1.0", "0.004")])
    assert total == 2.75 and [s["service"] for s in services] == ["EC2", "S3"]


class FakeCE:
    def __init__(self):
        self.calls = 0

    def get_cost_and_usage(self, **kw):
        self.calls += 1
        return {"ResultsByTime": [{"Groups": [{"Keys": ["EC2"], "Metrics": {"UnblendedCost": {"Amount": "3"}}}]}]}


def test_cost_explorer_is_called_once_then_cached():
    ce = FakeCE()
    c = make_clients(cost_explorer=ce)
    first = cost_tools.get_cost_by_service(c, 30)
    second = cost_tools.get_cost_by_service(c, 30)
    assert ce.calls == 1 and second["served_from_cache"] and not first["served_from_cache"]
    assert second["total_usd"] == 3.0


def test_cache_expires(tmp_path):
    clock = [1000.0]
    calls = []
    fetch = lambda: calls.append(1) or len(calls)
    assert cached("t", {"k": 1}, 60, fetch, now=lambda: clock[0]) == (1, False)
    assert cached("t", {"k": 1}, 60, fetch, now=lambda: clock[0] + 59) == (1, True)
    assert cached("t", {"k": 1}, 60, fetch, now=lambda: clock[0] + 61) == (2, False)


def test_every_result_has_window_and_timestamp():
    out = cost_tools._envelope(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 15, tzinfo=timezone.utc))
    assert set(out) >= {"window", "data_timestamp"} and out["window"]["days"] == 14.0


@mock_aws
def test_idle_ec2_with_too_little_data_is_too_new_not_idle():
    ec2 = boto3.client("ec2", region_name=REGION)
    cw = boto3.client("cloudwatch", region_name=REGION)
    ami = ec2.describe_images()["Images"][0]["ImageId"]
    iid = ec2.run_instances(ImageId=ami, InstanceType="t3.micro", MinCount=1, MaxCount=1)["Instances"][0]["InstanceId"]
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    dims = [{"Name": "InstanceId", "Value": iid}]
    # 2 hours of near-zero CPU and network: would look idle, but far too little data.
    cw.put_metric_data(Namespace="AWS/EC2", MetricData=[
        {"MetricName": m, "Dimensions": dims, "Timestamp": now - timedelta(hours=h), "Value": 0.1}
        for m in ("CPUUtilization", "NetworkIn", "NetworkOut") for h in (1, 2)])
    out = cost_tools.get_idle_ec2(make_clients(), days=14)
    assert out["idle_instances"] == [] and out["too_new_to_judge"] == [iid]

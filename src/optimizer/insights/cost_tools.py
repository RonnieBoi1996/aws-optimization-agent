"""Cost tools: short, pre-aggregated JSON summaries, never raw data (section 5).

Every result has `window` (what time range the numbers cover) and
`data_timestamp` (when we fetched them). Lists are capped so a big account
can't flood the model's context; `truncated` says how many were left out.

Each resource carries its IaC tags (iac_repo / iac_stack / service), so the
model can see what's managed. `validate` re-checks tags itself and never
trusts what the model repeats.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError

from optimizer.insights import pricing
from optimizer.insights.cache import cached

MAX_ITEMS = 25
IAC_TAG_KEYS = ("iac_repo", "iac_stack", "service", "Name", "environment")

# Idle = barely any CPU and barely any network for the whole window.
IDLE_MAX_AVG_CPU_PCT = 5.0
IDLE_MAX_PEAK_CPU_PCT = 20.0
IDLE_MAX_NETWORK_MB_PER_DAY = 5.0

COST_EXPLORER_TTL = 6 * 3600  # Cost Explorer charges per request; data updates ~daily anyway


@dataclass
class Clients:
    """boto3 clients the tools need. Built once per server from a verified session."""
    region: str
    ec2: Any
    cloudwatch: Any
    s3: Any
    cost_explorer: Any      # us-east-1
    compute_optimizer: Any
    pricing: Any            # us-east-1
    cloudwatch_for_region: Callable[[str], Any]  # S3 metrics live in the bucket's region


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _envelope(start: datetime, end: datetime, **body) -> dict:
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": round((end - start).total_seconds() / 86400, 1)},
        "data_timestamp": _now().isoformat(),
        **body,
    }


def _tags(tag_list: list[dict] | None) -> dict[str, str]:
    tags = {t["Key"]: t["Value"] for t in (tag_list or [])}
    return {k: tags[k] for k in IAC_TAG_KEYS if k in tags}


def _cap(items: list, key=None) -> tuple[list, int]:
    if key:
        items = sorted(items, key=key)
    return items[:MAX_ITEMS], max(len(items) - MAX_ITEMS, 0)


def _metric_stats(cloudwatch, namespace: str, metric: str, dims: list[dict], start: datetime,
                  end: datetime, period: int, stat: str) -> list[float]:
    resp = cloudwatch.get_metric_data(
        MetricDataQueries=[{
            "Id": "m",
            "MetricStat": {"Metric": {"Namespace": namespace, "MetricName": metric, "Dimensions": dims},
                           "Period": period, "Stat": stat},
        }],
        StartTime=start, EndTime=end,
    )
    return [float(v) for r in resp.get("MetricDataResults", []) for v in r.get("Values", [])]


# --------------------------------------------------------------------------- #
# get_idle_ec2
# --------------------------------------------------------------------------- #

def classify_idle(avg_cpu: float | None, peak_cpu: float | None, net_mb_per_day: float | None) -> bool:
    """True only when ALL signals are present and low. Missing data is never 'idle'."""
    if avg_cpu is None or peak_cpu is None or net_mb_per_day is None:
        return False
    return (avg_cpu < IDLE_MAX_AVG_CPU_PCT and peak_cpu < IDLE_MAX_PEAK_CPU_PCT
            and net_mb_per_day < IDLE_MAX_NETWORK_MB_PER_DAY)


def get_idle_ec2(c: Clients, days: int = 14) -> dict:
    days = max(1, min(days, 30))
    end = _now()
    start = end - timedelta(days=days)
    instances = []
    for page in c.ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for res in page["Reservations"]:
            instances.extend(res["Instances"])

    idle, checked = [], 0
    for inst in instances[:100]:  # bound CloudWatch calls on big accounts
        checked += 1
        dims = [{"Name": "InstanceId", "Value": inst["InstanceId"]}]
        hourly_avg = _metric_stats(c.cloudwatch, "AWS/EC2", "CPUUtilization", dims, start, end, 3600, "Average")
        net_in = _metric_stats(c.cloudwatch, "AWS/EC2", "NetworkIn", dims, start, end, 86400, "Sum")
        net_out = _metric_stats(c.cloudwatch, "AWS/EC2", "NetworkOut", dims, start, end, 86400, "Sum")
        avg_cpu = sum(hourly_avg) / len(hourly_avg) if hourly_avg else None
        peak_cpu = max(hourly_avg) if hourly_avg else None
        net_days = max(len(net_in), len(net_out))
        net_mb_day = ((sum(net_in) + sum(net_out)) / 1e6 / net_days) if net_days else None
        if not classify_idle(avg_cpu, peak_cpu, net_mb_day):
            continue
        hourly = pricing.ec2_hourly_usd(c.pricing, c.region, inst["InstanceType"])
        idle.append({
            "instance_id": inst["InstanceId"],
            "arn": _instance_arn(inst, c.region),
            "instance_type": inst["InstanceType"],
            "launch_time": inst["LaunchTime"].isoformat() if hasattr(inst["LaunchTime"], "isoformat") else str(inst["LaunchTime"]),
            "tags": _tags(inst.get("Tags")),
            "cpu_avg_pct": round(avg_cpu, 2),
            "cpu_peak_hourly_pct": round(peak_cpu, 2),
            "network_mb_per_day": round(net_mb_day, 3),
            "datapoints_hours": len(hourly_avg),
            "est_monthly_cost_usd": round(hourly * pricing.HOURS_PER_MONTH, 2) if hourly else None,
        })
    items, truncated = _cap(idle, key=lambda i: -(i["est_monthly_cost_usd"] or 0))
    return _envelope(start, end, criteria={
        "avg_cpu_below_pct": IDLE_MAX_AVG_CPU_PCT, "peak_hourly_cpu_below_pct": IDLE_MAX_PEAK_CPU_PCT,
        "network_below_mb_per_day": IDLE_MAX_NETWORK_MB_PER_DAY},
        running_instances_checked=checked, idle_instances=items, truncated=truncated)


def _instance_arn(inst: dict, region: str) -> str:
    owner = inst.get("OwnerId") or inst.get("NetworkInterfaces", [{}])[0].get("OwnerId", "")
    return f"arn:aws:ec2:{region}:{owner}:instance/{inst['InstanceId']}"


# --------------------------------------------------------------------------- #
# get_unattached_ebs
# --------------------------------------------------------------------------- #

def get_unattached_ebs(c: Clients, account_id: str) -> dict:
    now = _now()
    vols = []
    for page in c.ec2.get_paginator("describe_volumes").paginate(
            Filters=[{"Name": "status", "Values": ["available"]}]):
        vols.extend(page["Volumes"])

    out = []
    for v in vols:
        per_gb = pricing.ebs_gb_month_usd(c.pricing, c.region, v["VolumeType"])
        created = v["CreateTime"]
        out.append({
            "volume_id": v["VolumeId"],
            "arn": f"arn:aws:ec2:{c.region}:{account_id}:volume/{v['VolumeId']}",
            "size_gb": v["Size"],
            "volume_type": v["VolumeType"],
            "created": created.isoformat() if hasattr(created, "isoformat") else str(created),
            "age_days": (now - created).days if hasattr(created, "tzinfo") else None,
            "has_snapshot_source": bool(v.get("SnapshotId")),
            "tags": _tags(v.get("Tags")),
            # Storage only; provisioned IOPS/throughput extras aren't included.
            "est_monthly_cost_usd": round(per_gb * v["Size"], 2) if per_gb else None,
        })
    items, truncated = _cap(out, key=lambda i: -(i["est_monthly_cost_usd"] or 0))
    # "unattached right now" is a point-in-time fact, so the window is the moment we looked.
    return _envelope(now, now, note="EBS has no 'detached since' timestamp; age_days is time since creation.",
                     unattached_volumes=items, truncated=truncated)


# --------------------------------------------------------------------------- #
# get_rightsizing_recommendations (Compute Optimizer)
# --------------------------------------------------------------------------- #

def summarize_ec2_recommendation(r: dict) -> dict:
    options = sorted(r.get("recommendationOptions", []), key=lambda o: o.get("rank", 99))
    top = options[0] if options else {}
    savings = (top.get("savingsOpportunity") or {}).get("estimatedMonthlySavings") or {}
    util = {m["name"]: round(float(m["value"]), 2) for m in r.get("utilizationMetrics", [])
            if m.get("statistic") == "MAXIMUM"}
    return {
        "arn": r.get("instanceArn"),
        "instance_name": r.get("instanceName"),
        "finding": r.get("finding"),
        "finding_reasons": r.get("findingReasonCodes", []),
        "current_type": r.get("currentInstanceType"),
        "recommended_type": top.get("instanceType"),
        "performance_risk": top.get("performanceRisk"),
        "est_monthly_savings_usd": savings.get("value") if savings.get("currency", "USD") == "USD" else None,
        "max_utilization": util,
        "lookback_days": r.get("lookBackPeriodInDays"),
        "last_refresh": str(r.get("lastRefreshTimestamp")) if r.get("lastRefreshTimestamp") else None,
    }


def get_rightsizing_recommendations(c: Clients) -> dict:
    now = _now()
    try:
        status = c.compute_optimizer.get_enrollment_status().get("status")
        if status != "Active":
            return _envelope(now, now, status=status, recommendations=[],
                             note="Compute Optimizer isn't active for this account.")
        recs, token = [], None
        while True:
            kw = {"nextToken": token} if token else {}
            resp = c.compute_optimizer.get_ec2_instance_recommendations(**kw)
            recs.extend(resp.get("instanceRecommendations", []))
            token = resp.get("nextToken")
            if not token:
                break
    except ClientError as e:
        return _envelope(now, now, error=e.response["Error"]["Code"], recommendations=[])

    summaries = [summarize_ec2_recommendation(r) for r in recs]
    # Only the actionable ones; "Optimized" instances need no change.
    actionable = [s for s in summaries if s["finding"] in ("Overprovisioned", "Underprovisioned")]
    lookback = max((s["lookback_days"] or 14 for s in actionable), default=14)
    items, truncated = _cap(actionable, key=lambda s: -(s["est_monthly_savings_usd"] or 0))
    return _envelope(now - timedelta(days=lookback), now, instances_with_recommendations=len(summaries),
                     recommendations=items, truncated=truncated,
                     note=None if summaries else "No recommendations yet (needs ~30h of metrics after opt-in).")


# --------------------------------------------------------------------------- #
# get_storage_class_candidates
# --------------------------------------------------------------------------- #

STANDARD_MIN_GB_FOR_CANDIDATE = 1.0


def _bucket_facts(c: Clients, bucket: str, start: datetime, end: datetime) -> dict:
    loc = c.s3.get_bucket_location(Bucket=bucket).get("LocationConstraint") or "us-east-1"
    try:
        rules = c.s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
            raise
        rules = []
    try:
        tags = _tags(c.s3.get_bucket_tagging(Bucket=bucket).get("TagSet"))
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchTagSet":
            raise
        tags = {}
    cw = c.cloudwatch_for_region(loc)
    dims = [{"Name": "BucketName", "Value": bucket}, {"Name": "StorageType", "Value": "StandardStorage"}]
    sizes = _metric_stats(cw, "AWS/S3", "BucketSizeBytes", dims, start, end, 86400, "Average")
    objs = _metric_stats(cw, "AWS/S3", "NumberOfObjects",
                         [{"Name": "BucketName", "Value": bucket}, {"Name": "StorageType", "Value": "AllStorageTypes"}],
                         start, end, 86400, "Average")
    transitions = any(r.get("Status") == "Enabled" and (r.get("Transitions") or r.get("NoncurrentVersionTransitions"))
                      for r in rules)
    standard_gb = round(sizes[0] / 1e9, 3) if sizes else 0.0  # newest datapoint first
    return {
        "bucket": bucket,
        "arn": f"arn:aws:s3:::{bucket}",
        "region": loc,
        "tags": tags,
        "standard_storage_gb": standard_gb,
        "object_count": int(objs[0]) if objs else None,
        "lifecycle_rules": len(rules),
        "has_storage_class_transitions": transitions,
        "candidate": standard_gb >= STANDARD_MIN_GB_FOR_CANDIDATE and not transitions,
    }


def get_storage_class_candidates(c: Clients, bucket: str | None = None) -> dict:
    end = _now()
    start = end - timedelta(days=3)  # S3 storage metrics are daily; the newest point is enough
    if bucket:
        names = [bucket]
    else:
        names = [b["Name"] for b in c.s3.list_buckets().get("Buckets", [])][:50]
    facts, errors = [], []
    for name in names:
        try:
            facts.append(_bucket_facts(c, name, start, end))
        except ClientError as e:
            errors.append({"bucket": name, "error": e.response["Error"]["Code"]})
    candidates = [f for f in facts if f["candidate"]]
    items, truncated = _cap(candidates, key=lambda f: -f["standard_storage_gb"])
    return _envelope(start, end, buckets_checked=len(facts), candidates=items, truncated=truncated,
                     not_candidates=[f["bucket"] for f in facts if not f["candidate"]][:MAX_ITEMS],
                     errors=errors,
                     note=("Object access patterns aren't visible to this role (reading objects is denied), "
                           "so a candidate means 'large STANDARD storage with no transition rule', not 'cold data'."))


# --------------------------------------------------------------------------- #
# get_cost_by_service (Cost Explorer, cached)
# --------------------------------------------------------------------------- #

def summarize_cost(results: list[dict]) -> tuple[list[dict], float]:
    totals: dict[str, float] = {}
    for day in results:
        for g in day.get("Groups", []):
            totals[g["Keys"][0]] = totals.get(g["Keys"][0], 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
    total = sum(totals.values())
    services = [{"service": s, "cost_usd": round(v, 2)} for s, v in totals.items() if round(v, 2) > 0]
    services.sort(key=lambda s: -s["cost_usd"])
    return services, round(total, 2)


def get_cost_by_service(c: Clients, days: int = 30) -> dict:
    days = max(1, min(days, 90))
    end_d = _now().date()  # Cost Explorer's end date is exclusive
    start_d = end_d - timedelta(days=days)
    key = {"start": start_d.isoformat(), "end": end_d.isoformat(), "group": "SERVICE"}

    def fetch():
        results, token = [], None
        while True:
            kw = {"NextPageToken": token} if token else {}
            resp = c.cost_explorer.get_cost_and_usage(
                TimePeriod={"Start": key["start"], "End": key["end"]}, Granularity="DAILY",
                Metrics=["UnblendedCost"], GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}], **kw)
            results.extend(resp.get("ResultsByTime", []))
            token = resp.get("NextPageToken")
            if not token:
                return {"results": results, "fetched_at": _now().isoformat()}

    data, was_cached = cached("cost-explorer", key, COST_EXPLORER_TTL, fetch)
    services, total = summarize_cost(data["results"])
    items, truncated = _cap(services)
    start = datetime.combine(start_d, datetime.min.time(), timezone.utc)
    end = datetime.combine(end_d, datetime.min.time(), timezone.utc)
    env = _envelope(start, end, total_usd=total, services=items, truncated=truncated,
                    served_from_cache=was_cached)
    env["data_timestamp"] = data["fetched_at"]  # when Cost Explorer was actually asked
    return env


def stable_json(obj: Any) -> str:
    """Deterministic JSON (sorted keys) so identical data produces identical text."""
    return json.dumps(obj, sort_keys=True, default=str, indent=1)


def digest(obj: Any) -> str:
    return hashlib.sha256(stable_json(obj).encode()).hexdigest()[:12]

"""validate: turn what the model submitted into trusted Proposal records.

In order, for every submitted item:
1. Schema: must parse as AgentProposal (pydantic).
2. Evidence sanity: windows run forwards, nothing is dated in the future.
3. Protection: the optimizer's own identity and do_not_touch entries are dropped
   (rule 4; the rule engine's R1 uses the same function later).
4. Existence: the ARN must exist in AWS right now (catches invented ARNs).
5. IaC: service / iac_repo / iac_stack come from the resource's real tags.
   No iac tags -> UnmanagedFinding (reported, never a PR).

Everything that fails is kept as a `Rejected` record with the reason, which is
what the M9 report's "validation failure rate" counts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from botocore.exceptions import ClientError
from pydantic import ValidationError

from optimizer.context import LoadedContext
from optimizer.protection import protection_reason
from optimizer.schemas import AgentName, AgentProposal, Proposal, Rejected, UnmanagedFinding

ExistsFn = Callable[[str], bool | None]  # True / False / None = can't check this type
CLOCK_SKEW = timedelta(minutes=10)


def make_exists_fn(ec2: Any, s3: Any, account_id: str) -> ExistsFn:
    """Existence checks using only actions on the agent role's allow-list."""

    def exists(arn: str) -> bool | None:
        parts = arn.split(":", 5)
        service, acct, resource = parts[2], parts[4], parts[5]
        if acct and acct != account_id:
            return False  # another account's ARN is never valid here
        try:
            if service == "ec2" and resource.startswith("instance/"):
                res = ec2.describe_instances(InstanceIds=[resource.split("/", 1)[1]])["Reservations"]
                states = [i["State"]["Name"] for r in res for i in r["Instances"]]
                return bool(states) and all(s != "terminated" for s in states)
            if service == "ec2" and resource.startswith("volume/"):
                return bool(ec2.describe_volumes(VolumeIds=[resource.split("/", 1)[1]])["Volumes"])
            if service == "s3" and resource and "/" not in resource:
                s3.get_bucket_location(Bucket=resource)
                return True
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if "NotFound" in code or code in ("NoSuchBucket", "InvalidVolumeID.Malformed",
                                              "InvalidInstanceID.Malformed"):
                return False
            raise
        return None  # a resource type this check doesn't know

    return exists


def _evidence_problem(p: AgentProposal, now: datetime) -> str | None:
    for e in p.evidence:
        if e.window_start > e.window_end:
            return f"evidence '{e.metric}': window_start is after window_end"
        if e.data_timestamp > now + CLOCK_SKEW or e.window_end > now + CLOCK_SKEW:
            return f"evidence '{e.metric}': dated in the future"
    return None


def validate(raw_items: list[dict] | None, *, agent: AgentName, run_id: str, ctx: LoadedContext,
             exists: ExistsFn, now: datetime | None = None
             ) -> tuple[list[Proposal], list[UnmanagedFinding], list[Rejected]]:
    now = now or datetime.now(timezone.utc)
    proposals: list[Proposal] = []
    unmanaged: list[UnmanagedFinding] = []
    rejected: list[Rejected] = []
    seen: set[str] = set()

    def reject(reason: str, raw: dict, arn: str | None = None) -> None:
        rejected.append(Rejected(agent=agent, run_id=run_id, reason=reason, resource_arn=arn,
                                 raw=raw if isinstance(raw, dict) else {"value": str(raw)}))

    for n, raw in enumerate(raw_items or [], start=1):
        try:
            p = AgentProposal.model_validate(raw)
        except ValidationError as e:
            reject(f"schema: {e.error_count()} error(s): {e.errors()[0]['msg']} at "
                   f"{'.'.join(str(x) for x in e.errors()[0]['loc'])}", raw,
                   raw.get("resource_arn") if isinstance(raw, dict) else None)
            continue

        arn = p.resource_arn
        if arn in seen:
            reject("duplicate proposal for the same resource", raw, arn)
            continue
        seen.add(arn)

        if problem := _evidence_problem(p, now):
            reject(f"evidence: {problem}", raw, arn)
            continue

        if reason := protection_reason(arn, ctx.constraints_for(arn).do_not_touch):
            reject(f"protected (R1): {reason}", raw, arn)
            continue

        found = exists(arn)
        if found is False:
            reject("resource not found in AWS (invented or deleted ARN)", raw, arn)
            continue
        if found is None:
            reject("can't verify this resource type exists", raw, arn)
            continue

        tags = ctx.tag_map.get(arn, {})
        service = ctx.service_for(arn)
        pid = f"{agent}-{run_id}-{n:03d}"
        base = p.model_dump()
        if tags.get("iac_repo") and tags.get("iac_stack"):
            proposals.append(Proposal(**base, id=pid, agent=agent, run_id=run_id, service=service,
                                      iac_repo=tags["iac_repo"], iac_path=tags["iac_stack"]))
        else:
            unmanaged.append(UnmanagedFinding(**base, id=pid, agent=agent, run_id=run_id, service=service))

    return proposals, unmanaged, rejected

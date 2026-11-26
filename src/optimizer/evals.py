"""`optimizer eval`: score a shadow run against the seeded sandbox (M4).

Ground truth is evals/cases.yaml. Each case names a seeded resource by its
`eval_case` tag, so the scorer finds the real ARN at scoring time and nothing
account-specific is committed.

Outcomes per case:
  hit                  expected a proposal, got one                      (TP)
  miss                 expected a proposal, got none (or it was rejected) (FN)
  not_ready            no proposal yet, but the resource is younger than
                       min_age_hours, so the data didn't exist yet: not scored
  correctly_skipped    expected none, the agent proposed nothing
  caught_by_validator  expected none, the agent proposed it, `validate`
                       rejected it: safe, but the agent itself got it wrong
  false_positive       expected none, a proposal got through          (FP)
  not_seeded           no live resource has this eval_case tag: not scored

A proposal on any other sandbox resource (VPC, subnet, ...) is also a false
positive. Proposals outside the sandbox are listed as "unscored": we don't know
the right answer for the rest of the account.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from optimizer.config import REPO_ROOT

CASES_FILE = REPO_ROOT / "evals" / "cases.yaml"
EVAL_TAG = "eval_case"
SANDBOX_TAG = ("environment", "sandbox")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    agent: Literal["cost", "iam", "latency"]
    expect: Literal["proposal", "no_proposal"]
    min_age_hours: float = Field(default=0, ge=0)
    why: str = ""


class CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    cases: list[Case]


def load_cases(path: Path = CASES_FILE, agents: tuple[str, ...] = ("cost",)) -> list[Case]:
    with open(path, encoding="utf-8") as f:
        cases = CaseFile.model_validate(yaml.safe_load(f)).cases
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{path}: duplicate case ids")
    return [c for c in cases if c.agent in agents]


# --------------------------------------------------------------------------- #
# Finding the seeded resources (read-only AWS calls, agent role)
# --------------------------------------------------------------------------- #

@dataclass
class Seeded:
    arn: str
    created: datetime | None  # instance launch / volume creation time


@dataclass
class SandboxInventory:
    by_case: dict[str, Seeded]  # eval_case -> resource
    sandbox_arns: set[str]      # every live resource tagged environment=sandbox


def find_seeded(tagging: Any, ec2: Any) -> SandboxInventory:
    tagged: dict[str, dict[str, str]] = {}
    for page in tagging.get_paginator("get_resources").paginate(
            TagFilters=[{"Key": SANDBOX_TAG[0], "Values": [SANDBOX_TAG[1]]}], ResourcesPerPage=100):
        for r in page.get("ResourceTagMappingList", []):
            tagged[r["ResourceARN"]] = {t["Key"]: t["Value"] for t in r.get("Tags", [])}

    # The tagging API keeps listing terminated instances for a while, and it has
    # no creation time, so ask EC2 for both.
    created: dict[str, datetime] = {}
    gone: set[str] = set()
    inst_ids = {a.rsplit("/", 1)[1]: a for a in tagged if ":instance/" in a}
    vol_ids = {a.rsplit("/", 1)[1]: a for a in tagged if ":volume/" in a}
    if inst_ids:
        for page in ec2.get_paginator("describe_instances").paginate(
                Filters=[{"Name": "instance-id", "Values": list(inst_ids)}]):
            for res in page["Reservations"]:
                for i in res["Instances"]:
                    arn = inst_ids[i["InstanceId"]]
                    if i["State"]["Name"] in ("terminated", "shutting-down"):
                        gone.add(arn)
                    created[arn] = i["LaunchTime"]
    if vol_ids:
        for page in ec2.get_paginator("describe_volumes").paginate(
                Filters=[{"Name": "volume-id", "Values": list(vol_ids)}]):
            for v in page["Volumes"]:
                created[vol_ids[v["VolumeId"]]] = v["CreateTime"]
        gone |= {a for a in vol_ids.values() if a not in created}  # deleted volumes

    live = {a: t for a, t in tagged.items() if a not in gone}
    by_case = {t[EVAL_TAG]: Seeded(arn=a, created=created.get(a)) for a, t in live.items() if EVAL_TAG in t}
    return SandboxInventory(by_case=by_case, sandbox_arns=set(live))


# --------------------------------------------------------------------------- #
# Scoring (pure: no AWS, no model)
# --------------------------------------------------------------------------- #

Outcome = Literal["hit", "miss", "not_ready", "correctly_skipped", "caught_by_validator",
                  "false_positive", "not_seeded"]


@dataclass
class CaseResult:
    case: str
    expect: str
    outcome: Outcome
    arn: str | None = None
    detail: str = ""


@dataclass
class EvalResult:
    run_id: str
    results: list[CaseResult]
    unexpected: list[CaseResult]               # proposals on unseeded sandbox resources (FP)
    unscored: list[str] = field(default_factory=list)  # proposals outside the sandbox

    def count(self, *outcomes: str) -> int:
        return sum(r.outcome in outcomes for r in self.results + self.unexpected)

    @property
    def tp(self) -> int:
        return self.count("hit")

    @property
    def fp(self) -> int:
        return self.count("false_positive")

    @property
    def fn(self) -> int:
        return self.count("miss")

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def passed(self) -> bool:
        return self.fp == 0 and self.fn == 0

    def as_dict(self) -> dict:
        return {"run_id": self.run_id, "precision": self.precision, "recall": self.recall,
                "tp": self.tp, "fp": self.fp, "fn": self.fn, "passed": self.passed,
                "results": [asdict(r) for r in self.results],
                "unexpected": [asdict(r) for r in self.unexpected], "unscored": self.unscored}


def run_time_from_id(run_id: str) -> datetime:
    """Run IDs start with their UTC start time (logging_setup.new_run_id)."""
    return datetime.strptime(run_id.split("-", 1)[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def score(cases: list[Case], inv: SandboxInventory, run_id: str, proposals: list[dict],
          unmanaged: list[dict], rejected: list[dict]) -> EvalResult:
    run_time = run_time_from_id(run_id)
    proposed = {p["resource_arn"] for p in proposals + unmanaged}
    rejected_why = {r["resource_arn"]: r["reason"] for r in rejected if r.get("resource_arn")}

    results = []
    for c in cases:
        seeded = inv.by_case.get(c.id)
        if seeded is None:
            results.append(CaseResult(c.id, c.expect, "not_seeded", detail="no live resource has this eval_case tag"))
            continue
        arn = seeded.arn
        age_h = (run_time - seeded.created).total_seconds() / 3600 if seeded.created else None
        if c.expect == "proposal":
            if arn in proposed:
                results.append(CaseResult(c.id, c.expect, "hit", arn))
            elif age_h is not None and age_h < c.min_age_hours:
                results.append(CaseResult(c.id, c.expect, "not_ready", arn,
                                          f"{age_h:.0f}h old at run time, needs {c.min_age_hours:.0f}h"))
            else:
                detail = f"rejected: {rejected_why[arn]}" if arn in rejected_why else "not proposed"
                results.append(CaseResult(c.id, c.expect, "miss", arn, detail))
        else:
            if arn in proposed:
                results.append(CaseResult(c.id, c.expect, "false_positive", arn, "proposal got through validation"))
            elif arn in rejected_why:
                results.append(CaseResult(c.id, c.expect, "caught_by_validator", arn, rejected_why[arn]))
            else:
                results.append(CaseResult(c.id, c.expect, "correctly_skipped", arn))

    case_arns = {s.arn for s in inv.by_case.values()}
    unexpected = [CaseResult("(unseeded sandbox resource)", "no_proposal", "false_positive", a,
                             "sandbox infrastructure, not a fixture")
                  for a in sorted((proposed & inv.sandbox_arns) - case_arns)]
    unscored = sorted(proposed - inv.sandbox_arns)
    return EvalResult(run_id=run_id, results=results, unexpected=unexpected, unscored=unscored)


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def render(r: EvalResult) -> str:
    lines = [f"# Eval: run {r.run_id}", "",
             f"**Precision:** {_pct(r.precision)} · **Recall:** {_pct(r.recall)} · "
             f"TP {r.tp} · FP {r.fp} · FN {r.fn} · **{'PASS' if r.passed else 'FAIL'}**", "",
             "| Case | Expect | Outcome | Detail |", "| --- | --- | --- | --- |"]
    for c in r.results + r.unexpected:
        lines.append(f"| {c.case} | {c.expect} | {c.outcome} | {c.detail or (c.arn or '')} |")
    if r.unscored:
        lines += ["", f"Unscored proposals outside the sandbox: {len(r.unscored)} "
                      "(no ground truth for the rest of the account)."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# CLI entry
# --------------------------------------------------------------------------- #

def _latest_run(out_dir: Path) -> str:
    runs = sorted(p.name for p in out_dir.iterdir() if (p / "proposals.json").exists()) if out_dir.is_dir() else []
    if not runs:
        raise RuntimeError(f"no runs in {out_dir}; run `optimizer run` first, or use `optimizer eval --run`")
    return runs[-1]


def run_eval(run_id: str | None, run_first: bool) -> int:
    from optimizer.aws_session import agent_session
    from optimizer.config import load_config

    config = load_config()
    out_dir = config.resolve(config.paths.out_dir)
    if run_first:
        from optimizer.run import execute_shadow_run
        run_id = execute_shadow_run()
    run_id = run_id or _latest_run(out_dir)
    folder = out_dir / run_id
    if not (folder / "proposals.json").exists():
        raise RuntimeError(f"{folder} has no proposals.json")
    load = lambda name: json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))

    session = agent_session(config.aws.region, config.aws.account_id)
    inv = find_seeded(session.client("resourcegroupstaggingapi"), session.client("ec2"))
    result = score(load_cases(), inv, run_id, load("proposals"), load("unmanaged"), load("rejected"))

    (folder / "eval.json").write_text(json.dumps(result.as_dict(), indent=2, default=str), encoding="utf-8")
    text = render(result)
    (folder / "eval.md").write_text(text, encoding="utf-8")
    print("\n" + text)
    print(f"Saved: {folder}/eval.md")
    return 0 if result.passed else 1

"""Data shapes shared by every agent and graph node (section 4).

Two proposal shapes, on purpose:
- `AgentProposal` is what the model is allowed to say. It deliberately has no
  `service`, `iac_path`, `id` or plan addresses: those come from tags and code,
  never from the model, so the model can't invent them.
- `Proposal` is the validated record we store and act on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AgentName = Literal["cost", "iam", "latency"]
Category = Literal["security", "availability", "cost", "latency"]
Risk = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Proposals
# --------------------------------------------------------------------------- #

class Evidence(_Strict):
    """One measured fact backing a proposal. Must come from a tool result."""
    metric: str = Field(description="What was measured, e.g. 'CPUUtilization average'.")
    value: float | str
    unit: str | None = Field(default=None, description="e.g. 'Percent', 'GB', 'USD/month'.")
    window_start: datetime
    window_end: datetime
    data_timestamp: datetime = Field(description="When the tool fetched the data.")
    source_tool: str = Field(description="Name of the tool that returned this value.")


class EstimatedImpact(_Strict):
    monthly_cost_delta_usd: float = Field(description="Negative = saving, positive = extra cost.")
    p99_delta_ms: float | None = Field(default=None, description="Positive = slower.")
    permissions_removed: list[str] = Field(default_factory=list)


class AgentProposal(_Strict):
    """The fields an agent fills in. Everything else is added by code."""
    resource_arn: str
    category: Category
    change_summary: str = Field(min_length=10)
    rationale: str = Field(min_length=10)
    evidence: list[Evidence] = Field(min_length=1)
    estimated_impact: EstimatedImpact
    affects_availability: bool
    affects_availability_reason: str
    weakens_security: bool
    weakens_security_reason: str
    what_you_give_up: str = Field(min_length=3)
    reversible: bool
    requires_snapshot: bool
    risk_level: Risk
    current_p99_ms: float | None = None
    p99_data_timestamp: datetime | None = None

    @field_validator("resource_arn")
    @classmethod
    def _looks_like_arn(cls, v: str) -> str:
        if not v.startswith("arn:aws:") or v.count(":") < 5:
            raise ValueError(f"not an AWS ARN: {v!r}")
        return v


class Proposal(AgentProposal):
    """A validated proposal, ready for the rule engine (M7) and the synthesizer (M5)."""
    id: str
    agent: AgentName
    run_id: str
    service: str
    iac_path: str  # the Terraform stack, from the iac_stack tag
    iac_repo: str
    # Filled by the IaC synthesizer (M5), which reads the stack's code. Never by the model.
    expected_plan_addresses: list[str] = Field(default_factory=list)


class UnmanagedFinding(AgentProposal):
    """A finding on a resource with no IaC tags. Reported, never turned into a PR."""
    id: str
    agent: AgentName
    run_id: str
    service: str


class Rejected(_Strict):
    """Something the model proposed that didn't survive validation (and why)."""
    agent: AgentName
    run_id: str
    reason: str
    resource_arn: str | None = None
    raw: dict


# --------------------------------------------------------------------------- #
# constraints.yaml (one per service, in context/<service>/)
# --------------------------------------------------------------------------- #

class ThresholdOverrides(_Strict):
    max_p99_increase_ms: float | None = None
    max_sla_headroom_fraction: float | None = Field(default=None, ge=0, le=1)
    max_latency_fix_monthly_cost_usd: float | None = None


class ServiceConstraints(_Strict):
    owner: str
    criticality: Risk
    sla_p99_ms: float | None = None
    min_capacity: int | None = Field(default=None, ge=0)
    multi_az_required: bool = False
    dr_role: bool = False
    do_not_touch: list[str] = Field(default_factory=list, description="ARNs or fnmatch patterns.")
    notes: str = ""
    thresholds: ThresholdOverrides | None = None

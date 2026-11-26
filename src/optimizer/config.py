"""Load and validate config.yaml.

We validate with pydantic so a typo in config.yaml fails loudly at startup
instead of causing a confusing error halfway through a run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# Repo root = two levels above this file's package directory (src/optimizer/..).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


class _Strict(BaseModel):
    # forbid unknown keys: a misspelled key should be an error, not silently ignored
    model_config = ConfigDict(extra="forbid")


ACCOUNT_ID_ENV = "OPTIMIZER_AWS_ACCOUNT_ID"


class AwsConfig(_Strict):
    # Filled from the environment by load_config(), never from config.yaml,
    # because this repo is public. Exactly 12 digits, like every AWS account ID.
    account_id: str = Field(pattern=r"^\d{12}$")
    region: str
    cost_explorer_region: str = "us-east-1"


class ModelConfig(_Strict):
    id: str
    max_tokens: int = Field(gt=0)
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    refusal_fallback: bool = False


class ModelsConfig(_Strict):
    fast: ModelConfig
    smart: ModelConfig


class Pricing(_Strict):
    input: float
    output: float
    cache_write_5m: float
    cache_read: float


class Guardrails(_Strict):
    max_agent_turns: int = Field(default=10, gt=0)
    run_token_budget: int = Field(gt=0)
    dry_run_default: bool = True


class Thresholds(_Strict):
    max_p99_increase_ms: float
    max_sla_headroom_fraction: float = Field(ge=0, le=1)
    max_latency_fix_monthly_cost_usd: float


class Features(_Strict):
    access_analyzer_unused_access: bool = False


class Paths(_Strict):
    runs_dir: str = "runs"
    out_dir: str = "out"
    context_dir: str = "context"


class IacTarget(_Strict):
    # fnmatch patterns of stack directories (repo-relative) the synthesizer may edit.
    stacks: list[str] = Field(min_length=1)


class GithubConfig(_Strict):
    owner: str
    base_branch: str = "main"
    # iac_repo tag value -> what may be changed there. A repo missing here is never touched.
    targets: dict[str, IacTarget]
    mcp_image: str = Field(pattern=r"@sha256:[0-9a-f]{64}$")  # pinned by digest


class TerraformMcpConfig(_Strict):
    enabled: bool = True
    image: str = Field(pattern=r"@sha256:[0-9a-f]{64}$")


class SynthesizerConfig(_Strict):
    max_proposals_per_run: int = Field(default=5, ge=0)
    max_turns: int = Field(default=8, gt=0)  # includes retries after failed checks


class Config(_Strict):
    aws: AwsConfig
    models: ModelsConfig
    pricing_usd_per_mtok: dict[str, Pricing]
    guardrails: Guardrails
    thresholds: Thresholds
    features: Features = Features()
    paths: Paths = Paths()
    github: GithubConfig
    terraform_mcp: TerraformMcpConfig
    synthesizer: SynthesizerConfig = SynthesizerConfig()

    def resolve(self, relative: str) -> Path:
        """Turn a path from config (relative to the repo root) into an absolute path."""
        return REPO_ROOT / relative


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)  # safe_load: never execute YAML tags

    if "account_id" in raw.get("aws", {}):
        raise ValueError(f"Remove aws.account_id from {path}: this repo is public. "
                         f"Set the {ACCOUNT_ID_ENV} environment variable instead.")
    account_id = os.environ.get(ACCOUNT_ID_ENV, "").strip()
    if not account_id:
        raise RuntimeError(f"{ACCOUNT_ID_ENV} is not set. Export your 12-digit AWS account ID "
                           "(see README > Setup).")
    raw.setdefault("aws", {})["account_id"] = account_id
    return Config.model_validate(raw)

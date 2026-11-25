"""Thin wrapper around the Anthropic SDK: prompt layout, caching, usage, budget.

Why not LangChain's wrappers? Prompt caching is a byte-exact prefix match, so we
need full control over the request we send. This module owns that request shape.

Request layout (section 8), stable -> volatile:

    tools                      ┐
    system prompt              ┘  breakpoint 1  (same for every run of an agent)
    user[0].content[0]: service constraints + docs  breakpoint 2  (same per service)
    user[0].content[1]: the task, incl. run ID / dates     (volatile, never cached)
    ...assistant / tool_result turns...                    breakpoint 3 on the last
                                                           block (agent loops only)

At most 4 breakpoints are allowed per request; we use up to 3. All use the
default 5-minute TTL.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic

from optimizer.config import Config, ModelConfig, Pricing
from optimizer.logging_setup import RunLog

log = logging.getLogger(__name__)

ModelRole = Literal["fast", "smart"]
CACHE_5M = {"type": "ephemeral"}  # default TTL is 5 minutes
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class BudgetExceeded(RuntimeError):
    """Raised before a call that would start once the run's token budget is spent."""


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PromptPrefix:
    """The cacheable part of a prompt. Must contain NOTHING volatile.

    - tools:   fixed list per agent, same order and descriptions every run
    - system:  the agent's instructions (no dates, run IDs, timestamps)
    - context: constraints.yaml + docs for one service, already wrapped with
               scrub.untrusted_block(). Empty string if there is none.
    """
    tools: tuple[dict[str, Any], ...] = ()
    system: str = ""
    context: str = ""


def build_messages(prefix: PromptPrefix, task: str, history: list[dict] | None = None) -> list[dict]:
    """Build the messages list. `history` = turns after the first user message.

    We copy everything so adding cache_control never mutates the caller's history
    (a breakpoint left behind in an old turn would push us over the 4-breakpoint limit).
    """
    first_content: list[dict] = []
    if prefix.context:
        first_content.append({"type": "text", "text": prefix.context, "cache_control": CACHE_5M})
    first_content.append({"type": "text", "text": task})
    messages = [{"role": "user", "content": first_content}]

    if history:
        messages.extend(copy.deepcopy(history))
        # Breakpoint 3: cache the conversation so far, so the next loop turn
        # only pays full price for the newest tool results.
        last = messages[-1]
        if isinstance(last["content"], str):
            last["content"] = [{"type": "text", "text": last["content"]}]
        if last["content"]:
            last["content"][-1] = {**_as_dict(last["content"][-1]), "cache_control": CACHE_5M}
    return messages


def build_request(model: ModelConfig, prefix: PromptPrefix, task: str,
                  history: list[dict] | None = None) -> dict[str, Any]:
    """Return the full kwargs for messages.create(). Pure function = easy to test."""
    request: dict[str, Any] = {
        "model": model.id,
        "max_tokens": model.max_tokens,
        "output_config": {"effort": model.effort},
        "messages": build_messages(prefix, task, history),
    }
    if prefix.system:
        # Breakpoint 1 sits on the system block; because tools render before
        # system, this one marker caches tools + system together.
        request["system"] = [{"type": "text", "text": prefix.system, "cache_control": CACHE_5M}]
    if prefix.tools:
        request["tools"] = [copy.deepcopy(t) for t in prefix.tools]
    return request


def _as_dict(block: Any) -> dict:
    # Assistant turns may hold SDK objects (response.content); convert to plain dicts.
    return block.model_dump(exclude_none=True) if hasattr(block, "model_dump") else dict(block)


# --------------------------------------------------------------------------- #
# Usage tracking
# --------------------------------------------------------------------------- #

@dataclass
class AgentUsage:
    calls: int = 0
    input_tokens: int = 0            # uncached input, full price
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    output_tokens: int = 0
    est_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.cache_creation_input_tokens
                + self.cache_read_input_tokens + self.output_tokens)

    @property
    def cache_hit_ratio(self) -> float:
        """Share of all input tokens that were served from cache."""
        total_in = self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens
        return self.cache_read_input_tokens / total_in if total_in else 0.0


@dataclass
class UsageTracker:
    pricing: dict[str, Pricing]
    run_token_budget: int
    per_agent: dict[str, AgentUsage] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return sum(u.total_tokens for u in self.per_agent.values())

    def check_budget(self) -> None:
        if self.total_tokens >= self.run_token_budget:
            raise BudgetExceeded(
                f"Run token budget exhausted: {self.total_tokens:,} used of "
                f"{self.run_token_budget:,} (guardrails.run_token_budget in config.yaml). "
                "Stopping cleanly; no further model calls will be made.")

    def record(self, agent: str, model_id: str, usage: Any) -> AgentUsage:
        u = self.per_agent.setdefault(agent, AgentUsage())
        # `or 0`: the cache fields can be None when caching didn't apply
        inp = usage.input_tokens or 0
        cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cr = getattr(usage, "cache_read_input_tokens", 0) or 0
        out = usage.output_tokens or 0
        u.calls += 1
        u.input_tokens += inp
        u.cache_creation_input_tokens += cw
        u.cache_read_input_tokens += cr
        u.output_tokens += out
        price = self.pricing.get(model_id)
        if price is None:
            log.warning("No price configured for %s; cost estimate excludes this call", model_id)
        else:
            u.est_usd += (inp * price.input + cw * price.cache_write_5m
                          + cr * price.cache_read + out * price.output) / 1_000_000
        return u

    def summary_table(self) -> str:
        header = f"{'agent':<14}{'calls':>6}{'input':>10}{'cache_wr':>10}{'cache_rd':>10}{'output':>9}{'hit%':>7}{'est $':>10}"
        lines = [header, "-" * len(header)]
        total = AgentUsage()
        for name, u in sorted(self.per_agent.items()):
            lines.append(self._row(name, u))
            for f in ("calls", "input_tokens", "cache_creation_input_tokens",
                      "cache_read_input_tokens", "output_tokens", "est_usd"):
                setattr(total, f, getattr(total, f) + getattr(u, f))
        lines += ["-" * len(header), self._row("TOTAL", total)]
        return "\n".join(lines)

    @staticmethod
    def _row(name: str, u: AgentUsage) -> str:
        return (f"{name:<14}{u.calls:>6}{u.input_tokens:>10,}{u.cache_creation_input_tokens:>10,}"
                f"{u.cache_read_input_tokens:>10,}{u.output_tokens:>9,}"
                f"{u.cache_hit_ratio * 100:>6.1f}%{u.est_usd:>10.4f}")

    def as_dict(self) -> dict[str, Any]:
        return {name: {**u.__dict__, "cache_hit_ratio": round(u.cache_hit_ratio, 4)}
                for name, u in self.per_agent.items()}


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #

class LLM:
    """One per run. Shares a UsageTracker so the budget covers every agent."""

    def __init__(self, config: Config, run_log: RunLog | None = None,
                 client: anthropic.Anthropic | None = None):
        self.config = config
        self.run_log = run_log
        # Credentials come from the environment (ANTHROPIC_API_KEY); never hardcoded.
        self.client = client or anthropic.Anthropic()
        # Fail at startup with a clear message, not with a traceback on the first call.
        if client is None and not any(getattr(self.client, a, None)
                                      for a in ("api_key", "auth_token", "credentials")):
            raise RuntimeError("No Anthropic credentials found. Export ANTHROPIC_API_KEY "
                               "(see README > Setup).")
        self.usage = UsageTracker(pricing=config.pricing_usd_per_mtok,
                                  run_token_budget=config.guardrails.run_token_budget)

    def model(self, role: ModelRole) -> ModelConfig:
        return getattr(self.config.models, role)

    def call(self, *, agent: str, role: ModelRole, prefix: PromptPrefix, task: str,
             history: list[dict] | None = None):
        """Make one model call and record its usage. Returns the SDK Message.

        Callers running a tool loop must append `response.content` to history
        unchanged (it may contain thinking blocks the API expects back).
        """
        self.usage.check_budget()
        model = self.model(role)
        request = build_request(model, prefix, task, history)

        if model.refusal_fallback:
            response = self.client.beta.messages.create(
                **request, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            response = self.client.messages.create(**request)

        # Price by the model that actually answered (a fallback may have served it).
        agent_usage = self.usage.record(agent, response.model, response.usage)
        log.info("%s %s: in=%d cache_wr=%d cache_rd=%d out=%d stop=%s", agent, response.model,
                 response.usage.input_tokens or 0,
                 response.usage.cache_creation_input_tokens or 0,
                 response.usage.cache_read_input_tokens or 0,
                 response.usage.output_tokens or 0, response.stop_reason)
        if response.stop_reason == "refusal":
            log.warning("%s: model declined the request (stop_details=%s)", agent,
                        getattr(response, "stop_details", None))
        if self.run_log:
            self.run_log.event(
                "llm_call", agent=agent, requested_model=model.id, model=response.model,
                stop_reason=response.stop_reason,
                input_tokens=response.usage.input_tokens,
                cache_creation_input_tokens=response.usage.cache_creation_input_tokens,
                cache_read_input_tokens=response.usage.cache_read_input_tokens,
                output_tokens=response.usage.output_tokens,
                agent_cache_hit_ratio=round(agent_usage.cache_hit_ratio, 4))
        return response

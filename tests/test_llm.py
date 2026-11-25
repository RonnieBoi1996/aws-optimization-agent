"""Tests for llm.py. No network: a fake client stands in for the SDK."""

import json
from types import SimpleNamespace

import pytest

from optimizer.config import load_config
from optimizer.llm import LLM, BudgetExceeded, PromptPrefix, build_request
from optimizer.scrub import untrusted_block

TOOLS = (
    {"name": "get_idle_ec2", "description": "List idle EC2 instances.",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]}},
    {"name": "get_unattached_ebs", "description": "List unattached EBS volumes.",
     "input_schema": {"type": "object", "properties": {}}},
)


def _prefix(service="checkout"):
    return PromptPrefix(tools=TOOLS, system="You are the Cost agent.",
                        context=untrusted_block(f"context/{service}/constraints.yaml", "owner: team-a\nsla_p99_ms: 200"))


def _cached_prefix_bytes(request: dict) -> bytes:
    """Everything up to and including breakpoint 2: tools, system, context block."""
    stable = {"tools": request["tools"], "system": request["system"],
              "context": request["messages"][0]["content"][0]}
    return json.dumps(stable, sort_keys=False).encode()


def _count_breakpoints(obj) -> int:
    if isinstance(obj, dict):
        return ("cache_control" in obj) + sum(_count_breakpoints(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_count_breakpoints(v) for v in obj)
    return 0


@pytest.fixture
def config():
    return load_config()


def test_cached_prefix_is_byte_identical_across_runs(config):
    # Section 8.6: same service twice -> identical prefix, even though the
    # volatile task text (run ID, time) differs.
    a = build_request(config.models.fast, _prefix(), task="run A at 2026-01-01T00:00Z")
    b = build_request(config.models.fast, _prefix(), task="run B at 2026-06-30T12:34Z")
    assert _cached_prefix_bytes(a) == _cached_prefix_bytes(b)
    assert a["messages"][0]["content"][1] != b["messages"][0]["content"][1]


def test_volatile_task_is_after_last_prefix_breakpoint(config):
    req = build_request(config.models.fast, _prefix(), task="volatile")
    first = req["messages"][0]["content"]
    assert "cache_control" in first[0]          # context block is cached
    assert "cache_control" not in first[1]      # task block is not
    assert first[1]["text"] == "volatile"


def test_breakpoints_stay_within_limit_in_a_long_loop(config):
    history = []
    for i in range(5):
        history.append({"role": "assistant", "content": [{"type": "text", "text": f"turn {i}"}]})
        history.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]})
        req = build_request(config.models.fast, _prefix(), task="t", history=history)
        assert _count_breakpoints(req) <= 4
        assert "cache_control" in req["messages"][-1]["content"][-1]
    # and the caller's history was never mutated
    assert _count_breakpoints(history) == 0


def test_different_service_changes_only_context(config):
    a = build_request(config.models.fast, _prefix("checkout"), task="t")
    b = build_request(config.models.fast, _prefix("search"), task="t")
    assert a["tools"] == b["tools"] and a["system"] == b["system"]
    assert a["messages"][0]["content"][0] != b["messages"][0]["content"][0]


# --- fake client ----------------------------------------------------------- #

class _FakeMessages:
    def __init__(self, recorder, beta):
        self.recorder, self.beta = recorder, beta

    def create(self, **kwargs):
        self.recorder.append({"beta": self.beta, **kwargs})
        usage = SimpleNamespace(input_tokens=100, cache_creation_input_tokens=1000,
                                cache_read_input_tokens=3000, output_tokens=50)
        return SimpleNamespace(model=kwargs["model"], usage=usage, stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text="ok")])


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.messages = _FakeMessages(self.calls, beta=False)
        self.beta = SimpleNamespace(messages=_FakeMessages(self.calls, beta=True))


def test_usage_recorded_and_hit_ratio(config):
    llm = LLM(config, client=_FakeClient())
    llm.call(agent="cost", role="fast", prefix=_prefix(), task="t")
    u = llm.usage.per_agent["cost"]
    assert (u.calls, u.cache_read_input_tokens, u.total_tokens) == (1, 3000, 4150)
    assert u.cache_hit_ratio == pytest.approx(3000 / 4100)
    assert u.est_usd > 0
    assert "cost" in llm.usage.summary_table()


def test_fallback_only_for_models_that_enable_it(config):
    client = _FakeClient()
    llm = LLM(config, client=client)
    llm.call(agent="a", role="fast", prefix=_prefix(), task="t")
    llm.call(agent="a", role="smart", prefix=_prefix(), task="t")
    fast_call, smart_call = client.calls
    assert fast_call["beta"] is False and "fallbacks" not in fast_call
    assert smart_call["beta"] is True and smart_call["fallbacks"] == "default"


def test_budget_aborts_cleanly(config):
    config.guardrails.run_token_budget = 5000  # each fake call uses 4150 tokens
    llm = LLM(config, client=_FakeClient())
    llm.call(agent="a", role="fast", prefix=_prefix(), task="t")     # 4150 < 5000: allowed
    llm.call(agent="a", role="fast", prefix=_prefix(), task="t")     # starts below budget
    with pytest.raises(BudgetExceeded, match="budget exhausted"):
        llm.call(agent="a", role="fast", prefix=_prefix(), task="t")


def test_max_tokens_always_set(config):
    for role in ("fast", "smart"):
        req = build_request(getattr(config.models, role), _prefix(), task="t")
        assert req["max_tokens"] > 0

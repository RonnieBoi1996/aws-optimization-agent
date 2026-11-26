"""The tool-use loop every agent runs: model <-> aws-insights tools (MCP).

Flow per turn: call the model; run every tool it asked for through the MCP
client; send all results back in ONE user message; repeat. The loop ends when
the model calls `submit_proposals` (a local tool, not an AWS one), or when
max_agent_turns or the run's token budget is reached.

Tool results are untrusted AWS data: each one is scrubbed of secret-looking
values and wrapped in <untrusted_data> before the model sees it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from optimizer.llm import LLM, ModelRole, PromptPrefix
from optimizer.scrub import untrusted_block

log = logging.getLogger(__name__)

SUBMIT_TOOL_NAME = "submit_proposals"
MAX_TOOL_RESULT_CHARS = 30_000


@dataclass
class AgentResult:
    submitted: list[dict] | None  # None = the agent never submitted
    turns: int
    stop: str                      # "submitted" | "max_turns" | "no_tool_call" | "refusal"
    tool_calls: list[str] = field(default_factory=list)


def mcp_tools_to_anthropic(mcp_tools: list[Any]) -> list[dict]:
    """MCP tool definitions -> Anthropic tool definitions, sorted by name.

    Sorting makes the order independent of the server's internals, so the
    tool list is byte-identical on every run (section 8.3).
    """
    out = []
    for t in sorted(mcp_tools, key=lambda t: t.name):
        schema = dict(t.input_schema or {"type": "object", "properties": {}})
        schema.pop("title", None)
        out.append({"name": t.name, "description": t.description or "", "input_schema": schema})
    return out


def inline_refs(schema: dict) -> dict:
    """Replace pydantic's $ref/$defs with the definitions themselves."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def submit_tool(item_schema: dict, description: str) -> dict:
    return {
        "name": SUBMIT_TOOL_NAME,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"proposals": {"type": "array", "items": inline_refs(item_schema)}},
            "required": ["proposals"],
        },
    }


def _tool_result_text(result: Any) -> tuple[str, bool]:
    texts = [getattr(b, "text", "") for b in result.content if getattr(b, "type", "") == "text"]
    text = "\n".join(texts) or json.dumps(result.structured_content or {}, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n... [truncated]"
    return text, bool(result.is_error)


async def run_tool_loop(*, llm: LLM, agent: str, role: ModelRole, prefix: PromptPrefix,
                        task: str, mcp_client: Any, max_turns: int) -> AgentResult:
    history: list[dict] = []
    calls: list[str] = []
    reminded = False

    for turn in range(1, max_turns + 1):
        response = llm.call(agent=agent, role=role, prefix=prefix, task=task, history=history)
        # Append the assistant turn UNCHANGED (it may hold thinking blocks the API expects back).
        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            return AgentResult(None, turn, "refusal", calls)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            if reminded:
                return AgentResult(None, turn, "no_tool_call", calls)
            reminded = True
            history.append({"role": "user", "content": [{"type": "text", "text":
                f"Finish by calling {SUBMIT_TOOL_NAME} (with an empty list if there is nothing to propose)."}]})
            continue

        results, submitted = [], None
        for tu in tool_uses:
            calls.append(tu.name)
            if tu.name == SUBMIT_TOOL_NAME:
                submitted = list((tu.input or {}).get("proposals", []))
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "Received."})
                continue
            try:
                result = await mcp_client.call_tool(tu.name, tu.input or {})
                text, is_error = _tool_result_text(result)
            except Exception as e:  # a broken tool must not crash the run
                text, is_error = f"tool failed: {type(e).__name__}: {e}", True
            log.info("%s: tool %s -> %d chars%s", agent, tu.name, len(text), " (error)" if is_error else "")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": is_error,
                            "content": untrusted_block(f"tool:{tu.name}", text)})

        # All results of this turn go back in ONE message (keeps parallel tool use working).
        history.append({"role": "user", "content": results})
        if submitted is not None:
            return AgentResult(submitted, turn, "submitted", calls)

    return AgentResult(None, max_turns, "max_turns", calls)

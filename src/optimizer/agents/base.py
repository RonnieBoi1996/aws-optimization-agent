"""The tool-use loop every agent runs: model <-> aws-insights tools (MCP).

Flow per turn: call the model; run every tool it asked for through the MCP
client; send all results back in ONE user message; repeat. The loop ends when
the model calls its submit tool (a local tool, e.g. `submit_proposals`) and
code accepts the submission, or when
max_agent_turns or the run's token budget is reached.

Tool results are untrusted AWS data: each one is scrubbed of secret-looking
values and wrapped in <untrusted_data> before the model sees it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from optimizer.llm import LLM, ModelRole, PromptPrefix
from optimizer.scrub import scrub, untrusted_block

log = logging.getLogger(__name__)

SUBMIT_TOOL_NAME = "submit_proposals"
MAX_TOOL_RESULT_CHARS = 30_000


@dataclass
class AgentResult:
    submitted: list[dict] | None  # None = the agent never submitted
    turns: int
    stop: str                      # "submitted" | "max_turns" | "no_tool_call" | "refusal"
    tool_calls: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # [{resource_arn, reason}] the agent chose not to propose
    submit_input: dict | None = None  # the accepted submit call's full input


# Returns None to accept a submission, or an error message to send back to the
# model (it then gets another turn to fix it, within max_turns).
SubmitCheck = Callable[[dict], str | None]


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
            "properties": {
                "proposals": {"type": "array", "items": inline_refs(item_schema)},
                # Not acted on by code: it's for humans and the eval, so a miss can be
                # told apart from "looked at it and decided against it".
                "skipped": {
                    "type": "array",
                    "description": "Resources a tool flagged that you decided NOT to propose, with why.",
                    "items": {"type": "object", "additionalProperties": False,
                              "properties": {"resource_arn": {"type": "string"}, "reason": {"type": "string"}},
                              "required": ["resource_arn", "reason"]},
                },
            },
            "required": ["proposals"],
        },
    }


def clean_skipped(raw: Any, limit: int = 50) -> list[dict]:
    """Keep only well-formed {resource_arn, reason} items, bounded and scrubbed (model output)."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("resource_arn"), str) and isinstance(item.get("reason"), str):
            out.append({"resource_arn": item["resource_arn"][:300], "reason": scrub(item["reason"][:500])})
    return out[:limit]


def _tool_result_text(result: Any) -> tuple[str, bool]:
    texts = [getattr(b, "text", "") for b in result.content if getattr(b, "type", "") == "text"]
    text = "\n".join(texts) or json.dumps(result.structured_content or {}, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "\n... [truncated]"
    return text, bool(result.is_error)


async def run_tool_loop(*, llm: LLM, agent: str, role: ModelRole, prefix: PromptPrefix,
                        task: str, mcp_client: Any, max_turns: int,
                        submit_name: str = SUBMIT_TOOL_NAME,
                        check_submit: SubmitCheck | None = None) -> AgentResult:
    history: list[dict] = []
    calls: list[str] = []
    reminded = False

    for turn in range(1, max_turns + 1):
        response = llm.call(agent=agent, role=role, prefix=prefix, task=task, history=history)
        # Append the assistant turn UNCHANGED (it may hold thinking blocks the API expects back).
        history.append({"role": "assistant", "content": response.content})
        # Keep what the model said and submitted in the run log (RunLog scrubs it):
        # without this, a missed finding can't be explained after the fact.
        if getattr(llm, "run_log", None):
            text = "\n".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
            submit = [b.input for b in response.content
                      if getattr(b, "type", "") == "tool_use" and b.name == submit_name]
            llm.run_log.event("assistant_turn", agent=agent, turn=turn, text=text[:4000],
                              submit_input=submit[0] if submit else None)

        if response.stop_reason == "refusal":
            return AgentResult(None, turn, "refusal", calls)

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            if reminded:
                return AgentResult(None, turn, "no_tool_call", calls)
            reminded = True
            history.append({"role": "user", "content": [{"type": "text", "text":
                f"Finish by calling {submit_name}."}]})
            continue

        results, accepted = [], None
        for tu in tool_uses:
            calls.append(tu.name)
            if tu.name == submit_name:
                payload = dict(tu.input or {})
                problem = check_submit(payload) if check_submit else None
                if problem:  # code rejected it: say why and let the model try again
                    log.info("%s: %s rejected by checks", agent, submit_name)
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                    "content": f"Not accepted. Fix these problems and call {submit_name} "
                                               f"again:\n{problem}"})
                else:
                    accepted = payload
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
        if accepted is not None:
            return AgentResult(list(accepted.get("proposals", []) or []), turn, "submitted", calls,
                               clean_skipped(accepted.get("skipped")), accepted)

    return AgentResult(None, max_turns, "max_turns", calls)

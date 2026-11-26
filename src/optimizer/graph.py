"""The LangGraph workflow (shadow mode):

    load_context -> cost_agent -> validate -> synthesize -> write_output

`synthesize` (M5) writes each managed proposal's Terraform change, manifest and
PR description to out/<run_id>/changes/. It never opens a PR: that's
`optimizer pr`, for one proposal a human picked.

Later milestones add the IAM and Latency agents in parallel, conflict
detection, the rule engine, the human gate and the IaC synthesizer.

State holds only plain, JSON-serializable data, so the SQLite checkpointer
(M7) can save and resume it. Things that can't be serialized (the LLM client,
boto3 clients, the run log) live in `Runtime` and reach nodes through closures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from optimizer.agents.cost import AGENT as COST, run_cost_agent
from optimizer.config import Config
from optimizer.context import DEFAULT_SERVICE, LoadedContext, ServiceContext, fetch_tag_map, load_context
from optimizer.llm import LLM
from optimizer.logging_setup import RunLog
from optimizer.output import write_outputs
from optimizer.schemas import ServiceConstraints
from optimizer.tracing import traced_node
from optimizer.validate import ExistsFn, validate

log = logging.getLogger(__name__)


class RunState(TypedDict, total=False):
    run_id: str
    tag_map: dict[str, dict[str, str]]
    constraints: dict[str, dict]       # service -> ServiceConstraints dump
    context_text: str
    raw_proposals: list[dict] | None
    skipped: list[dict]
    agent_stop: str
    proposals: list[dict]
    unmanaged: list[dict]
    rejected: list[dict]
    output_dir: str
    changes: list[dict]                # synthesizer outcome per proposal


@dataclass
class Runtime:
    config: Config
    llm: LLM
    run_log: RunLog
    tagging: Any            # boto3 resourcegroupstaggingapi client
    exists: ExistsFn
    mcp_server: Any = None  # tests: in-process MCP server instead of the subprocess
    synthesizer: Any = None  # iac.synthesizer.Synthesizer, or None to skip synthesis


def _ctx_from_state(state: RunState) -> LoadedContext:
    services = {name: ServiceContext(name, ServiceConstraints.model_validate(c))
                for name, c in state["constraints"].items()}
    return LoadedContext(tag_map=state["tag_map"], services=services, context_text=state["context_text"])


def build_graph(rt: Runtime):
    cfg = rt.config

    @traced_node("load_context")
    def node_load_context(state: RunState) -> RunState:
        tag_map = fetch_tag_map(rt.tagging)
        ctx = load_context(cfg.resolve(cfg.paths.context_dir), tag_map)
        rt.run_log.event("context_loaded", tagged_resources=len(tag_map),
                         services=sorted(ctx.services), context_chars=len(ctx.context_text))
        log.info("context: %d tagged resources, services: %s", len(tag_map), ", ".join(sorted(ctx.services)))
        return {"tag_map": tag_map, "context_text": ctx.context_text,
                "constraints": {n: s.constraints.model_dump(mode="json") for n, s in ctx.services.items()}}

    @traced_node("cost_agent")
    async def node_cost_agent(state: RunState) -> RunState:
        result = await run_cost_agent(llm=rt.llm, config=cfg, run_id=state["run_id"],
                                      context_text=state["context_text"], mcp_server=rt.mcp_server)
        rt.run_log.event("agent_finished", agent=COST, stop=result.stop, turns=result.turns,
                         tool_calls=result.tool_calls,
                         submitted=len(result.submitted) if result.submitted is not None else None,
                         skipped=result.skipped)
        return {"raw_proposals": result.submitted, "agent_stop": result.stop, "skipped": result.skipped}

    @traced_node("validate")
    def node_validate(state: RunState) -> RunState:
        proposals, unmanaged, rejected = validate(
            state.get("raw_proposals"), agent=COST, run_id=state["run_id"],
            ctx=_ctx_from_state(state), exists=rt.exists)
        for p in proposals:
            rt.run_log.event("proposal", **p.model_dump(mode="json"))
        for u in unmanaged:
            rt.run_log.event("unmanaged_finding", **u.model_dump(mode="json"))
        for r in rejected:
            rt.run_log.event("validation_failure", **r.model_dump(mode="json"))
        return {"proposals": [p.model_dump(mode="json") for p in proposals],
                "unmanaged": [u.model_dump(mode="json") for u in unmanaged],
                "rejected": [r.model_dump(mode="json") for r in rejected]}

    @traced_node("synthesize")
    async def node_synthesize(state: RunState) -> RunState:
        if rt.synthesizer is None:
            return {"changes": []}
        from optimizer.iac.synthesizer import write_change  # lazy: only runs that synthesize need it
        out_dir = cfg.resolve(cfg.paths.out_dir)
        limit = cfg.synthesizer.max_proposals_per_run
        changes = []
        for i, p in enumerate(state["proposals"]):
            if i >= limit:
                changes.append({"proposal_id": p["id"], "status": "skipped",
                                "reason": f"over synthesizer.max_proposals_per_run ({limit})"})
                continue
            try:
                r = await rt.synthesizer.synthesize(p, state["tag_map"].get(p["resource_arn"], {}))
            except Exception as e:  # one broken proposal must not lose the others
                log.exception("synthesizer failed on %s", p["id"])
                changes.append({"proposal_id": p["id"], "status": "failed", "reason": f"{type(e).__name__}: {e}"})
                continue
            folder = write_change(out_dir, state["run_id"], p, r, cfg.aws.account_id)
            rt.run_log.event("synthesized", proposal_id=p["id"], status=r.status, reason=r.reason,
                             stack=r.stack, plan_changes=r.plan_changes)
            changes.append({"proposal_id": p["id"], "status": r.status, "reason": r.reason,
                            "stack": r.stack, "repo": r.repo, "plan_changes": r.plan_changes,
                            "folder": str(folder)})
        return {"changes": changes}

    @traced_node("write_output")
    def node_write_output(state: RunState) -> RunState:
        folder = write_outputs(cfg.resolve(cfg.paths.out_dir), state["run_id"], state["proposals"],
                               state["unmanaged"], state["rejected"], state.get("agent_stop", "?"),
                               rt.llm.usage.summary_table(), skipped=state.get("skipped", []),
                               changes=state.get("changes", []))
        return {"output_dir": str(folder)}

    g = StateGraph(RunState)
    g.add_node("load_context", node_load_context)
    g.add_node("cost_agent", node_cost_agent)
    g.add_node("validate", node_validate)
    g.add_node("synthesize", node_synthesize)
    g.add_node("write_output", node_write_output)
    g.add_edge(START, "load_context")
    g.add_edge("load_context", "cost_agent")
    g.add_edge("cost_agent", "validate")
    g.add_edge("validate", "synthesize")
    g.add_edge("synthesize", "write_output")
    g.add_edge("write_output", END)
    return g.compile()


__all__ = ["build_graph", "Runtime", "RunState", "DEFAULT_SERVICE"]

"""`optimizer run`: one shadow-mode run of the graph."""

from __future__ import annotations

import asyncio
import logging

from optimizer.aws_session import agent_session
from optimizer.config import load_config
from optimizer.graph import Runtime, build_graph
from optimizer.llm import LLM
from optimizer.logging_setup import RunLog, new_run_id, setup_logging
from optimizer.tracing import setup_tracing
from optimizer.validate import make_exists_fn

log = logging.getLogger(__name__)


def run_shadow() -> int:
    execute_shadow_run()
    return 0


def execute_shadow_run() -> str:
    """Run the graph once in shadow mode; return the run ID (out/<run_id>/)."""
    setup_logging()
    setup_tracing()
    config = load_config()
    session = agent_session(config.aws.region, config.aws.account_id)  # refuses non-agent identities

    run_id = new_run_id()
    run_log = RunLog(config.resolve(config.paths.runs_dir), run_id)
    llm = LLM(config, run_log=run_log)
    rt = Runtime(config=config, llm=llm, run_log=run_log,
                 tagging=session.client("resourcegroupstaggingapi"),
                 exists=make_exists_fn(session.client("ec2"), session.client("s3"), config.aws.account_id))

    run_log.event("run_start", mode="shadow", agents=["cost"])
    try:
        final = asyncio.run(build_graph(rt).ainvoke({"run_id": run_id}))
    except Exception as e:
        run_log.event("run_failed", error=f"{type(e).__name__}: {e}", usage=llm.usage.as_dict())
        print("\n" + llm.usage.summary_table())
        raise
    run_log.event("run_end", usage=llm.usage.as_dict(), proposals=len(final["proposals"]),
                  unmanaged=len(final["unmanaged"]), rejected=len(final["rejected"]))

    print(f"\nRun {run_id}: {len(final['proposals'])} proposals, {len(final['unmanaged'])} unmanaged "
          f"findings, {len(final['rejected'])} rejected (agent: {final.get('agent_stop')}).")
    print(f"Summary:  {final['output_dir']}/summary.md")
    print(f"Run log:  {run_log.path}\n")
    print(llm.usage.summary_table())
    return run_id

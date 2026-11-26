"""`optimizer run`: one shadow-mode run of the graph."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from optimizer.aws_session import agent_session
from optimizer.config import load_config
from optimizer.graph import Runtime, build_graph
from optimizer.llm import LLM
from optimizer.logging_setup import RunLog, new_run_id, setup_logging
from optimizer.tracing import setup_tracing
from optimizer.validate import make_exists_fn

log = logging.getLogger(__name__)


def run_shadow(synthesize: bool = True) -> int:
    execute_shadow_run(synthesize)
    return 0


def execute_shadow_run(synthesize: bool = True) -> str:
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

    workdir = Path(tempfile.mkdtemp(prefix="optimizer-run-"))  # target repo clones; deleted below
    if synthesize:
        from optimizer.iac.synthesizer import Synthesizer
        rt.synthesizer = Synthesizer(config, llm, run_id, workdir)

    run_log.event("run_start", mode="shadow", agents=["cost"], synthesize=synthesize)
    try:
        final = asyncio.run(build_graph(rt).ainvoke({"run_id": run_id}))
    except Exception as e:
        run_log.event("run_failed", error=f"{type(e).__name__}: {e}", usage=llm.usage.as_dict())
        print("\n" + llm.usage.summary_table())
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    run_log.event("run_end", usage=llm.usage.as_dict(), proposals=len(final["proposals"]),
                  unmanaged=len(final["unmanaged"]), rejected=len(final["rejected"]))

    print(f"\nRun {run_id}: {len(final['proposals'])} proposals, {len(final['unmanaged'])} unmanaged "
          f"findings, {len(final['rejected'])} rejected (agent: {final.get('agent_stop')}).")
    for c in final.get("changes", []):
        print(f"  change {c['proposal_id']}: {c['status']}" + (f" ({c['reason']})" if c.get("reason") else ""))
    print(f"Summary:  {final['output_dir']}/summary.md")
    print(f"Run log:  {run_log.path}\n")
    print(llm.usage.summary_table())
    return run_id


def open_pr(run_id: str, proposal_id: str) -> int:
    """`optimizer pr`: publish one already-synthesized change (no model calls)."""
    from optimizer.iac.publisher import publish
    setup_logging()
    config = load_config()
    run_log = RunLog(config.resolve(config.paths.runs_dir), run_id)  # appended to that run's log
    info = asyncio.run(publish(config, run_id, proposal_id))
    run_log.event("pr_" + info["status"], proposal_id=proposal_id, url=info["url"], branch=info["branch"],
                  github_calls=info["calls"])
    verb = "Opened" if info["status"] == "opened" else "Already open"
    print(f"{verb}: {info['url']}  (branch {info['branch']})")
    return 0

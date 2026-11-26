"""aws-insights cost server: the Cost agent's tools, over MCP (stdio).

Started by the Cost agent node as a subprocess:
    python -m optimizer.insights.cost_server

On startup it checks it is running as optimizer-agent-read and exits otherwise.
Tool names, order and descriptions are fixed text, so the tool list is
byte-identical on every run (prompt caching, section 8).
"""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from optimizer.aws_session import agent_session
from optimizer.config import load_config
from optimizer.insights import cost_tools
from optimizer.insights.cost_tools import Clients
from optimizer.insights.pricing import PRICING_REGION
from optimizer.logging_setup import setup_logging

log = logging.getLogger(__name__)


def build_server(clients: Clients, account_id: str) -> MCPServer:
    server = MCPServer("aws-insights-cost", instructions="Read-only AWS cost summaries.")

    @server.tool(description=(
        "Running EC2 instances that look idle over the last `days` days (default 14): "
        "average CPU below 5%, peak hourly CPU below 20% and network below 5 MB/day. "
        "Returns metrics, IaC tags and an estimated on-demand monthly cost per instance."))
    def get_idle_ec2(days: int = 14) -> dict:
        return cost_tools.get_idle_ec2(clients, days)

    @server.tool(description=(
        "EBS volumes not attached to any instance (status 'available'), with size, type, "
        "age, IaC tags and estimated monthly storage cost."))
    def get_unattached_ebs() -> dict:
        return cost_tools.get_unattached_ebs(clients, account_id)

    @server.tool(description=(
        "AWS Compute Optimizer EC2 rightsizing recommendations for over- or under-provisioned "
        "instances: current and recommended type, performance risk, estimated monthly savings "
        "and peak utilization."))
    def get_rightsizing_recommendations() -> dict:
        return cost_tools.get_rightsizing_recommendations(clients)

    @server.tool(description=(
        "S3 buckets with at least 1 GB in STANDARD storage and no storage-class transition "
        "rule. Pass `bucket` to check one bucket, or omit it to check all buckets. Object "
        "contents and access patterns are not visible."))
    def get_storage_class_candidates(bucket: str | None = None) -> dict:
        return cost_tools.get_storage_class_candidates(clients, bucket)

    @server.tool(description=(
        "Unblended cost per AWS service over the last `days` days (default 30), from Cost "
        "Explorer. Cached for 6 hours because every Cost Explorer request is billed."))
    def get_cost_by_service(days: int = 30) -> dict:
        return cost_tools.get_cost_by_service(clients, days)

    return server


def clients_from_session(session, region: str) -> Clients:
    return Clients(
        region=region,
        ec2=session.client("ec2"),
        cloudwatch=session.client("cloudwatch"),
        s3=session.client("s3"),
        cost_explorer=session.client("ce", region_name="us-east-1"),
        compute_optimizer=session.client("compute-optimizer"),
        pricing=session.client("pricing", region_name=PRICING_REGION),
        cloudwatch_for_region=lambda r: session.client("cloudwatch", region_name=r),
    )


def main() -> int:
    setup_logging()  # stderr only: stdout carries the MCP protocol
    config = load_config()
    session = agent_session(config.aws.region, config.aws.account_id)  # exits if not agent role
    server = build_server(clients_from_session(session, config.aws.region), config.aws.account_id)
    log.info("aws-insights-cost ready")
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())

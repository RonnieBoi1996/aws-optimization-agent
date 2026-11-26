"""aws-insights: our own read-only MCP servers (section 5).

One server per agent, so each agent's tool list stays small and fixed:
  cost_server  ->  Cost agent   (M3)
  iam_server   ->  IAM agent    (M6)
  latency_server -> Latency agent (M8)

Tool logic lives in plain functions (cost_tools.py etc.) that take boto3
clients, so it can be unit-tested with moto; the *_server.py files only wire
those functions up as MCP tools.
"""

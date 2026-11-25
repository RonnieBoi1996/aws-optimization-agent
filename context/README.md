# context/: what the agents know about each service

One folder per value of the `service` tag:

```text
context/
  _default/constraints.yaml     used for untagged resources and unknown services
  <service>/constraints.yaml    required
  <service>/docs/*.md           optional: runbooks, post-mortems, ADRs
```

`constraints.yaml` fields (validated by `ServiceConstraints` in `src/optimizer/schemas.py`;
unknown keys are rejected):

| Field | Meaning |
| --- | --- |
| `owner` | who to ask |
| `criticality` | low / medium / high |
| `sla_p99_ms` | latency SLA, or null if none |
| `min_capacity` | never go below this many instances/tasks |
| `multi_az_required` | changes must keep multi-AZ |
| `dr_role` | resource is part of disaster recovery: don't trim |
| `do_not_touch` | ARNs or patterns agents must never propose changes to |
| `notes` | free text |
| `thresholds` | optional overrides of `config.yaml` thresholds |

Everything here is passed to the model as quoted **data**, never as instructions.

## Tags every Terraform stack must set

```hcl
default_tags {
  tags = {
    iac_repo  = "infra-live"      # or aws-optimization-agent for sandbox/
    iac_stack = "stacks/app"      # the stack directory
    service   = "checkout"        # -> context/checkout/
  }
}
```

Resources without `iac_repo` + `iac_stack` count as **unmanaged**: they're reported,
never turned into PRs.

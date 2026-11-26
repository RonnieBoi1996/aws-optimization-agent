# sandbox/: test fixtures for the optimizer

Isolated by tags (`environment = sandbox`) and its own state key
(`sandbox/terraform.tfstate`), because there's no separate sandbox account.
Applied only by the CI pipeline, after a merged PR.

## M4 eval fixtures

| Name | What | Eval case | The agent should |
| --- | --- | --- | --- |
| `sandbox-web-1` | t3.nano, no workload | `idle-ec2` | propose stopping or removing it |
| `sandbox-data-1` | 1 GB gp3, attached to nothing | `unattached-ebs` | propose deleting it (with a snapshot) |
| `sandbox-worker-1` | t3.small, ~7% CPU from `files/load.sh` | `oversized-ec2` | propose a smaller type |
| `sandbox-archive-vol` | 1 GB gp3, attached to nothing, in `do_not_touch` | `do-not-touch-ebs` | leave it alone |
| `sandbox-report-builder` | same as the worker, but a post-mortem says don't downsize | `postmortem-ec2` | leave it alone |

The ground truth is in [evals/cases.yaml](../evals/cases.yaml). The names are neutral
on purpose: the agent sees Name tags, so a name like "idle-instance" would give
the answer away.

The instances sit in a private VPC with no internet gateway and a security group
with no rules: nothing reaches them and they reach nothing.

## Cost

About **$1.30/day** while the fixtures exist (eu-west-1 on-demand):

| Item | $/hour | $/day |
| --- | --- | --- |
| t3.nano | 0.0057 | 0.14 |
| 2 × t3.small | 0.0456 | 1.09 |
| 26 GB gp3 (3 × 8 GB roots + 2 × 1 GB) | | 0.08 |

VPC, subnet and security groups are free. The instances use `standard` CPU
credits, so they can't run up surprise burst charges. One eval cycle takes ~2
days because Compute Optimizer needs ~30 hours of metrics, so that's about $3–4
in total if you destroy right after.

## Destroy

```bash
scripts/sandbox_destroy.sh
```

Uses your admin SSO session, shows the plan and asks for `yes`. The code still
describes the fixtures, so the next merged sandbox PR recreates them; delete
`fixtures.tf` and `network.tf` in a PR to retire them for good.

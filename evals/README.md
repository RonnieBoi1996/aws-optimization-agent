# evals/: does the agent find what it should, and only that?

[cases.yaml](cases.yaml) is the ground truth for the fixtures seeded by
[sandbox/fixtures.tf](../sandbox/fixtures.tf). Each case names a resource by its
`eval_case` tag, so no account IDs or resource IDs are committed.

```bash
AWS_PROFILE=agent-dev uv run optimizer eval --run     # fresh shadow run, then score it (~$0.02–0.05)
AWS_PROFILE=agent-dev uv run optimizer eval           # score the newest run in out/
AWS_PROFILE=agent-dev uv run optimizer eval --run-id 20261201T120000Z-abc123
```

It prints a table, saves `out/<run_id>/eval.md` and `eval.json`, and exits 1 if
there's any false positive or miss.

## How it scores

| Outcome | Meaning | Counts as |
| --- | --- | --- |
| `hit` | expected a proposal, got one | TP |
| `miss` | expected a proposal, got none or it was rejected | FN |
| `not_ready` | no proposal, but the resource was younger than `min_age_hours`: the data didn't exist yet | not scored |
| `correctly_skipped` | expected none, the agent proposed nothing | correct |
| `caught_by_validator` | expected none; the agent proposed it, but `validate` rejected it | correct, but worth a look: the agent ignored a constraint |
| `false_positive` | expected none, a proposal got through | FP |
| `not_seeded` | no live resource carries this tag | not scored |

**Precision** = TP / (TP + FP): of what the agent proposed in the sandbox, how much
was right. **Recall** = TP / (TP + FN): of the real problems, how many it found.
A proposal on any other sandbox resource (the VPC, say) is also a false positive.
Proposals on resources outside the sandbox are listed as "unscored", because
there's no ground truth for the rest of the account.

The eval checks *which resources* got proposals, not the exact wording of the
change. The proposal text is in `out/<run_id>/summary.md` for a human to judge.

## Timing

- The two "leave it alone" cases can be scored right after apply.
- `idle-ec2` and `unattached-ebs` need 24 h (the idle tool needs 24 hourly datapoints;
  a volume detached for minutes isn't waste yet).

The model isn't deterministic: a borderline case can go either way between two runs
of the same data. Each miss shows the agent's own reason (from its `skipped` list),
so you can tell a bad judgement from a missing fact.
- `oversized-ec2` needs Compute Optimizer: ~30 h of metrics, then a daily refresh, so ~48 h.

Compute Optimizer is a black box. If it decides the worker is "Optimized" (it
might, for a burstable instance), that case shows up as a miss. The agent isn't
at fault there; the fix would be a heavier fixture.

The post-mortem in `context/sandbox-reports/docs/` is a fixture too: it describes
an incident that didn't happen, written so the agent has a documented reason not
to downsize `sandbox-report-builder`.

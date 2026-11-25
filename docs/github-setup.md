# GitHub setup for the Terraform pipeline

Applies to both repos (`aws-optimization-agent` and `infra-live`). Everything
below is applied by [scripts/github_setup.sh](../scripts/github_setup.sh), so
this page explains *what* it sets and *why*.

```bash
export OPTIMIZER_AWS_ACCOUNT_ID=...   # already in your env file
aws sso login --profile AdministratorAccess-$OPTIMIZER_AWS_ACCOUNT_ID
scripts/github_setup.sh RonnieBoi1996/aws-optimization-agent all
scripts/github_setup.sh RonnieBoi1996/infra-live all
```

## Rulesets on `main`

Two rulesets, because GitHub doesn't let you approve your own PRs and you are
the only human reviewer.

| Ruleset | Rules | Who can bypass |
| --- | --- | --- |
| **main: PR + checks (no bypass)** | No direct pushes, no force pushes, no branch deletion. Every change needs a PR whose `required` check passed. The check must come from the GitHub Actions app (`integration_id` 15368), so nothing else can fake a green `required` status. | **Nobody**, including you and admins |
| **main: code-owner approval** | 1 approving review from a code owner (`.github/CODEOWNERS`), stale approvals dismissed on new pushes, and the last push must be approved by someone other than its author | Repo admins (you), **only through a PR** (`bypass_mode: pull_request`) |

What this means in practice:

- **Agent PRs** (opened by the GitHub App from M5) need your approval and green
  checks. The App has no bypass and no merge rights.
- **Your own PRs** need green checks. GitHub won't let you approve them yourself,
  so you merge using the bypass ("Merge without waiting for requirements to be
  met" appears for the approval rule only). The checks still can't be skipped.
- **Nobody can push to `main` directly.** That includes Claude Code working on
  this repo: from now on, every change arrives through a PR.

### The `required` check

The workflow runs one job per step per stack (`fmt (sandbox)`, `plan (infra)`, ...)
so you can see exactly which step failed. Those names change with the stacks a PR
touches, so they can't be listed as required checks one by one. Instead, the
`required` job waits for all of them and fails if any step failed or was
cancelled. It also fails if a fork PR changes Terraform, because forks never get
AWS credentials, so they can't be planned. It's the only check the ruleset needs.

## Environment `production`

- **Required reviewer: you.** Every apply run pauses until you click *Approve*.
  Self-review is allowed, since you're the only reviewer.
- **Deployment branches: `main` only.** A job from any other branch can't enter
  the environment, so it can't get the `tf-apply` role (whose OIDC trust accepts
  only `environment:production`).
- **`TF_APPLY_ROLE_ARN` is an environment secret**, so only jobs that passed the
  approval can even see it.

## Secrets

| Secret | Scope | Used for |
| --- | --- | --- |
| `AWS_ACCOUNT_ID` | repo | `allowed-account-ids` (refuse credentials for any other account) and masking in PR comments |
| `TF_STATE_BUCKET` | repo | `terraform init -backend-config` |
| `TF_PLAN_ROLE_ARN` | repo | PR plans |
| `TF_APPLY_ROLE_ARN` | environment `production` | applies |
| `ALERT_EMAIL` | repo (this repo only) | planning `infra/` without proposing to delete the email subscriptions |

None of these are truly secret (account IDs and role ARNs aren't credentials),
but storing them as secrets keeps them out of the public repo and masks them in
public workflow logs. There are **no AWS keys anywhere**: GitHub Actions gets
short-lived credentials through OIDC.

## Actions settings

- **Default `GITHUB_TOKEN` permission: read-only.** Each job asks for exactly what
  it needs (`permissions:` per job in the workflows).
- **Actions can't approve pull requests**, so a workflow can't approve its own PR.
- **Fork PRs need approval to run workflows**, for every outside contributor. In
  a public repo anyone can open a PR. Fork PRs never get secrets or OIDC tokens,
  but approval also stops them from running anything on our runners unasked.

## Why the pipeline is safe to merge into

- Scripts that judge a PR (`ci/`, `.tflint.hcl`) are checked out from the **base**
  branch, so a PR can't weaken its own checks.
- Workflow files changed in a PR *do* run (that's how GitHub's `pull_request`
  event works). Two things cover this:
  - the CODEOWNERS review: any change under `.github/` needs your approval;
  - the agent's GitHub App gets no `workflows` permission, and GitHub refuses
    pushes that touch `.github/workflows/` without it.
- The full plan JSON (which can contain secret values) never leaves the runner.
  Jobs pass each other only addresses and actions, plus the human-readable plan
  with the account ID masked.
- checkov runs from an empty directory, so a `.checkov.yaml` added in a PR can't
  turn checks off. Skips are inline only, and each must give a reason.

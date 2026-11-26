# AWS Optimization Agent

A multi-agent system that analyzes one AWS account (region `eu-west-1`) and
proposes **cost**, **least-privilege IAM** and **latency** improvements, delivered
only as Terraform pull requests. A GitHub Actions pipeline checks each PR, and a
human approves it before anything is applied.

The full spec is in [CLAUDE_CODE_PROMPT.md](CLAUDE_CODE_PROMPT.md). This README
grows with each milestone.

## Safety model, in one paragraph

The agents only ever **read** AWS, under a read-only IAM role. The only thing they
write is a PR on an `optimizer/*` branch. Only the CI pipeline applies Terraform,
using its own role, after a human merges. Conflicts between agents are settled by
a plain-Python rule engine, never by a model, and anything unclear is escalated to
a human.

## Status

| Milestone | State |
| --- | --- |
| M0 Scaffold (config, logging, LLM wrapper with caching + usage tracking) | done |
| M1 Bootstrap infra (`infra/`): state bucket, OIDC, 4 roles, boundaries, CloudTrail, alerts | done (applied 2026-11-25) |
| M2 Terraform PR + apply pipeline (`.github/`, `ci/`, `pipelines/`, `sandbox/`) | done (first CI apply 2026-11-25, PR #2) |
| M3 `aws-insights` cost tools + Cost agent, shadow mode | done (2026-11-25, PR #4) |
| M4 Sandbox fixtures (`sandbox/fixtures.tf`) + eval (`evals/`, `optimizer eval`) | done (2026-11-26, PRs #5, #6); full eval due ~2026-11-28 |
| M5 IaC synthesizer + `optimizer pr` (GitHub App, GitHub + Terraform MCP servers) | in review |

## Setup

Prerequisites (already installed on the dev machine): Python 3.12, [uv](https://docs.astral.sh/uv/), Docker (runs the GitHub and Terraform MCP servers),
Terraform, AWS CLI v2, tflint, checkov, gh.

```bash
uv sync                                  # create .venv and install dependencies
uv run pytest                            # offline tests, no key or account needed
uv run optimizer smoke                   # 2 Haiku calls; costs well under $0.01
```

### Environment variables

This repo is public, so nothing account-specific is committed. Set these in your
shell instead:

| Variable | What | Secret? |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API key (Console, project workspace with a spend limit) | Yes |
| `OPTIMIZER_AWS_ACCOUNT_ID` | 12-digit AWS account ID the optimizer is allowed to analyze | No, but kept out of the repo |
| `OPTIMIZER_GITHUB_APP_ID`, `OPTIMIZER_GITHUB_APP_INSTALLATION_ID` | GitHub App that opens PRs ([docs/github-app.md](docs/github-app.md)); its private key is a file, `~/.config/aws-optimizer/github-app.pem` | IDs no, key yes |

The recommended place is a file outside the repo, readable only by you, loaded
from `~/.zshrc`:

```bash
# ~/.config/aws-optimizer/anthropic.env   (chmod 600)
export ANTHROPIC_API_KEY=sk-ant-...
export OPTIMIZER_AWS_ACCOUNT_ID=123456789012

# ~/.zshrc
[ -f ~/.config/aws-optimizer/anthropic.env ] && source ~/.config/aws-optimizer/anthropic.env
```

`.env` is git-ignored as a safety net, but don't rely on it. `config.yaml` refuses
to load if someone adds an `account_id` to it.

### What `smoke` should print

```text
call 1: answer='250'  cache_wr=12342  cache_rd=0      uncached_in=68
call 2: answer='250'  cache_wr=0      cache_rd=12342  uncached_in=68
```

Call 1 writes the prompt cache and call 2 reads it. If call 2 shows `cache_rd=0`,
the cached prefix changed between the two calls (see "Prompt caching" below).

## Running the Cost agent (shadow mode)

```bash
aws sso login --profile AdministratorAccess-$OPTIMIZER_AWS_ACCOUNT_ID   # the agent role is assumed from it
AWS_PROFILE=agent-dev uv run optimizer run
```

It refuses to start unless AWS says it is `optimizer-agent-read` in your account, so it
can't accidentally run with admin rights. Nothing is changed in AWS and no PRs are opened.
Results go to `out/<run_id>/`:

| File | Contents |
| --- | --- |
| `summary.md` | human-readable proposals, unmanaged findings, rejections, token usage |
| `proposals.json` | validated proposals for Terraform-managed resources |
| `unmanaged.json` | findings on resources without IaC tags (never turned into PRs) |
| `rejected.json` | what the model proposed that failed validation, and why |

Every event also goes to `runs/<run_id>.jsonl`.

How a run flows:

```text
load_context   tags (tag:GetResources) -> service -> context/<service>/constraints.yaml + docs
cost_agent     Sonnet + aws-insights cost tools (MCP subprocess), ends with submit_proposals
validate       schema -> evidence dates -> protected/do_not_touch -> exists in AWS -> IaC tags
write_output   out/<run_id>/
```

The model never decides `service`, `iac_path` or plan addresses: those come from the
resource's real tags (and, in M5, from the Terraform code). See [context/README.md](context/README.md)
for the tags every stack must set.

### Cost tools (`aws-insights`)

| Tool | What it returns |
| --- | --- |
| `get_cost_by_service(days)` | Cost Explorer spend per service (cached 6 h: every request is billed $0.01) |
| `get_idle_ec2(days)` | running instances with avg CPU < 5 %, peak hourly CPU < 20 %, network < 5 MB/day, with ≥ 24 h of data (newer instances are listed as `too_new_to_judge`) |
| `get_unattached_ebs()` | volumes in state `available`, with size, age and monthly cost |
| `get_rightsizing_recommendations()` | Compute Optimizer over/under-provisioned instances |
| `get_storage_class_candidates(bucket)` | buckets with ≥ 1 GB STANDARD and no transition rule. `bucket` is optional (all buckets), because untagged buckets don't show up in tag lookups |

Every result includes its time `window` and a `data_timestamp`. Prices come from the AWS
Pricing API (cached a week); when a price can't be found it's reported as unknown, never guessed.

## Evaluating the agent (M4)

`sandbox/` seeds five known cases (an idle instance, an unattached volume, an
oversized instance, a `do_not_touch` volume, and an instance a post-mortem says not
to downsize). `optimizer eval` scores a run against them:

```bash
AWS_PROFILE=agent-dev uv run optimizer eval --run     # run, then score (precision / recall)
scripts/sandbox_destroy.sh                            # tear the fixtures down (~$1.30/day while up)
```

Details: [evals/README.md](evals/README.md) and [sandbox/README.md](sandbox/README.md).

## Terraform changes and PRs (M5)

Every `optimizer run` also writes, for each managed proposal, the Terraform change that
would implement it. **Nothing is pushed.**

```text
out/<run_id>/changes/<proposal_id>/
  change.diff          the edit, after terraform fmt
  manifest.json        what the plan may touch (the CI scope guard enforces it)
  pr_title.txt, pr_description.md
  result.json          status (ok / gave_up / refused / failed), checks log
```

How a change is made (`src/optimizer/iac/`):

1. **Where:** the resource's `iac_repo` / `iac_stack` tags pick the repo and stack, but only
   stacks allowed in `config.yaml` `github.targets`. In this repo only `sandbox/` is ever
   editable, whatever the config says (`paths.py`): `infra/`, `.github/` and `ci/` define
   the optimizer's own identity and checks.
2. **What:** Sonnet reads the stack's `.tf` files, looks up provider docs through the
   Terraform MCP server when unsure, and submits exact search-and-replace edits plus the
   one plan address it expects to change.
3. **Checks (code, `checks.py`):** edits stay inside the stack, `.tf` only; exactly one plan
   address, and it exists in the code; no IAM; a volume that needs a snapshot can only be
   deleted once `final_snapshot = true` is already applied (otherwise the change is "step 1:
   set it"); `terraform fmt` + `validate`. Failures go back to the model to fix.

To open one as a real PR, after reading its `pr_description.md`:

```bash
uv run optimizer pr --run-id <run_id> --proposal <proposal_id>
```

No model is involved: it re-applies the edits on a fresh clone of `main`, re-runs every
check, then makes three GitHub calls (branch, one commit with the `.tf` files and the
manifest, PR) through the official GitHub MCP server, as the GitHub App
([docs/github-app.md](docs/github-app.md)). The account ID is masked in everything sent to
GitHub. Until the M7 human gate exists, choosing the proposal yourself is the gate.

## Layout

```text
config.yaml               models, thresholds, guardrails, pricing (edit this, not code)
src/optimizer/
  config.py               loads + validates config.yaml (typos fail loudly)
  llm.py                  Anthropic SDK wrapper: prompt layout, cache breakpoints,
                          usage tracking, per-run token budget
  scrub.py                secret redaction + <untrusted_data> quoting
  logging_setup.py        console logging + runs/<run_id>.jsonl event log
  smoke.py, cli.py        `uv run optimizer smoke`, `uv run optimizer run`
  schemas.py              proposal + constraints.yaml schemas (pydantic)
  context.py              load_context: tags -> service -> constraints + docs
  validate.py             schema, evidence, protection, existence, IaC tags
  protection.py           the optimizer's own identity + do_not_touch (rule 4, in code)
  aws_session.py          refuses to run as anything but optimizer-agent-read
  graph.py, run.py        LangGraph workflow and the `run` command
  evals.py                `optimizer eval`: score a run against evals/cases.yaml
  iac/                    synthesizer (Terraform edit + checks), publisher (`optimizer pr`), GitHub App auth
  agents/                 tool-use loop (base.py) and the Cost agent (cost.py)
  insights/               aws-insights MCP server (cost_server.py) + tool logic
tests/                    pytest, fully offline (fake Anthropic client)
runs/, out/               run logs and dry-run outputs (git-ignored)
context/                  per-service constraints.yaml + docs (see context/README.md)
infra/                    bootstrap Terraform stack, applied by hand with admin (see infra/README.md)
sandbox/                  test fixtures for the agents; applied by CI (tagged environment=sandbox)
evals/                    ground truth for the sandbox fixtures (cases.yaml)
.github/workflows/        terraform-pr.yml (checks + plan + comment), terraform-apply.yml
ci/                       stdlib-only pipeline scripts: stack detection, plan summary,
                          scope guard, PR comment, masking
pipelines/                copy-ready pipeline for infra-live (generated by pipelines/sync.py)
scripts/github_setup.sh   repo settings, environment, secrets, rulesets (docs/github-setup.md)
scripts/sandbox_destroy.sh  destroy the sandbox stack (you run it, with admin SSO)
.tflint.hcl               tflint config shared by all stacks (AWS ruleset)
```

## Key design decisions (M0)

**Model roles, not model names.** Code asks for `fast` (Haiku 5.5: data gathering and
summarizing) or `smart` (Sonnet 5.5: reasoning, Terraform, briefs). The mapping lives in
`config.yaml`.

**Prompt caching.** Caching only works when the start of the prompt is byte-for-byte
identical between calls, so `llm.py` always builds requests in this order:

1. tools + system prompt → **cache breakpoint 1** (identical for every run of an agent)
2. the service's `constraints.yaml` + docs → **cache breakpoint 2** (identical per service)
3. the task, which carries the run ID, dates and anything else that changes → never cached
4. in agent loops, a **breakpoint 3** on the newest turn, so earlier turns are read from cache

Never put timestamps, run IDs or dates in a system prompt or tool description:
that silently turns caching off. `tests/test_llm.py` checks that the prefix stays
byte-identical. The API allows at most 4 breakpoints, and the minimum cacheable
prefix is 512 tokens on Haiku 5.5 and Sonnet 5.5.

**Usage and budget.** Every call records input, cache-write, cache-read and output
tokens per agent. `UsageTracker.summary_table()` prints these plus the hit ratio and
an estimated cost; prices are in `config.yaml`. Before each call the wrapper checks
`guardrails.run_token_budget`. Once it's exhausted, the run stops with a clear
`BudgetExceeded` message, never halfway through a call.

**Refusal fallback.** Sonnet calls set `fallbacks="default"` (beta
`server-side-fallback-2026-07-01`). If the model declines a request for policy reasons,
the API retries it on a fallback model inside the same call. Turn this off per model with
`refusal_fallback: false`.

**Untrusted data.** All AWS data, tags, repo files and docs go through `scrub()` (which
redacts AWS keys, tokens, passwords, PEM keys and URL credentials) and are wrapped in
`<untrusted_data>` tags before they reach a prompt. Every `runs/*.jsonl` event is
scrubbed too.

## Planned integrations

- **Terraform MCP server** (HashiCorp, official): gives the model live Terraform
  Registry docs for providers and modules, so it checks real resource arguments
  instead of guessing (rule 9). It will be wired into the agents that write
  Terraform (the IaC synthesizer, M5) and read-only, like every other agent tool.
- **GitHub MCP server** (official): used only by the IaC synthesizer node, to open PRs.

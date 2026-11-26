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
| M3 `aws-insights` cost tools + Cost agent, shadow mode | in review |

## Setup

Prerequisites (already installed on the dev machine): Python 3.12, [uv](https://docs.astral.sh/uv/),
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
| `get_idle_ec2(days)` | running instances with avg CPU < 5 %, peak hourly CPU < 20 %, network < 5 MB/day |
| `get_unattached_ebs()` | volumes in state `available`, with size, age and monthly cost |
| `get_rightsizing_recommendations()` | Compute Optimizer over/under-provisioned instances |
| `get_storage_class_candidates(bucket)` | buckets with ≥ 1 GB STANDARD and no transition rule. `bucket` is optional (all buckets), because untagged buckets don't show up in tag lookups |

Every result includes its time `window` and a `data_timestamp`. Prices come from the AWS
Pricing API (cached a week); when a price can't be found it's reported as unknown, never guessed.

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
  agents/                 tool-use loop (base.py) and the Cost agent (cost.py)
  insights/               aws-insights MCP server (cost_server.py) + tool logic
tests/                    pytest, fully offline (fake Anthropic client)
runs/, out/               run logs and dry-run outputs (git-ignored)
context/                  per-service constraints.yaml + docs (see context/README.md)
infra/                    bootstrap Terraform stack, applied by hand with admin (see infra/README.md)
sandbox/                  test fixtures for the agents; applied by CI (tagged environment=sandbox)
.github/workflows/        terraform-pr.yml (checks + plan + comment), terraform-apply.yml
ci/                       stdlib-only pipeline scripts: stack detection, plan summary,
                          scope guard, PR comment, masking
pipelines/                copy-ready pipeline for infra-live (generated by pipelines/sync.py)
scripts/github_setup.sh   repo settings, environment, secrets, rulesets (docs/github-setup.md)
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

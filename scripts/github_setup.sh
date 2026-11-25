#!/usr/bin/env bash
# Configure a GitHub repo for the Terraform pipeline (section 7). Idempotent.
#
#   scripts/github_setup.sh <owner/repo> <step>...
#
# Steps:
#   settings     Actions: read-only default token, Actions can't approve PRs,
#                fork PR workflows need approval for every outside contributor
#   environment  `production` environment: you must approve, main branch only
#   secrets      repo + environment secrets, read from AWS / infra/ outputs
#                (needs an admin SSO session; values are never printed)
#   rulesets     protect main (see docs/github-setup.md for what each rule does)
#   all          everything above, in that order
#
# Needs: gh (logged in as the repo admin), aws, terraform, run from the repo root.

set -euo pipefail

REPO="${1:?usage: $0 <owner/repo> <step>...}"
shift
[ $# -gt 0 ] || { echo "usage: $0 <owner/repo> settings|environment|secrets|rulesets|all" >&2; exit 2; }

ADMIN_PROFILE="${ADMIN_PROFILE:-AdministratorAccess-${OPTIMIZER_AWS_ACCOUNT_ID:?set OPTIMIZER_AWS_ACCOUNT_ID}}"
GITHUB_ACTIONS_APP_ID=15368 # the GitHub Actions app; pins the required check to it
REPO_ADMIN_ROLE_ID=5        # built-in "Repository admin" role, used for the review bypass

say() { printf '\n== %s\n' "$*"; }

step_settings() {
  say "Actions settings"
  gh api -X PUT "repos/$REPO/actions/permissions/workflow" --silent --input - <<'EOF'
{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": false}
EOF
  gh api -X PUT "repos/$REPO/actions/permissions/fork-pr-contributor-approval" --silent --input - <<'EOF'
{"approval_policy": "all_external_contributors"}
EOF
  gh api "repos/$REPO/actions/permissions/workflow"
}

step_environment() {
  say "Environment: production"
  local me
  me=$(gh api user -q .id)
  gh api -X PUT "repos/$REPO/environments/production" --silent --input - <<EOF
{
  "reviewers": [{"type": "User", "id": $me}],
  "prevent_self_review": false,
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
EOF
  # Only the main branch may deploy to production.
  if ! gh api "repos/$REPO/environments/production/deployment-branch-policies" \
        -q '.branch_policies[].name' | grep -qx main; then
    gh api -X POST "repos/$REPO/environments/production/deployment-branch-policies" \
      -f name=main -f type=branch --silent
  fi
  gh api "repos/$REPO/environments/production" -q '{name, reviewers: [.protection_rules[]?.reviewers[]?.reviewer.login], branches: .deployment_branch_policy}'
}

# Pipe a value into `gh secret set` so it never appears in argv or output.
set_secret() { # name value [environment]
  if [ -z "$2" ]; then echo "  skip $1 (empty)"; return; fi
  if [ -n "${3:-}" ]; then
    printf '%s' "$2" | gh secret set "$1" --repo "$REPO" --env "$3"
  else
    printf '%s' "$2" | gh secret set "$1" --repo "$REPO"
  fi
  echo "  set $1${3:+ (environment: $3)}"
}

step_secrets() {
  say "Secrets (values are read from AWS and never printed)"
  local outputs account alert_email=""
  outputs=$(AWS_PROFILE="$ADMIN_PROFILE" terraform -chdir=infra output -json)
  account=$(AWS_PROFILE="$ADMIN_PROFILE" aws sts get-caller-identity --query Account --output text)

  set_secret AWS_ACCOUNT_ID "$account"
  set_secret TF_STATE_BUCKET "$(jq -r .state_bucket_name.value <<<"$outputs")"
  set_secret TF_PLAN_ROLE_ARN "$(jq -r .role_arns.value.tf_plan <<<"$outputs")"
  set_secret TF_APPLY_ROLE_ARN "$(jq -r .role_arns.value.tf_apply <<<"$outputs")" production

  # Only this repo plans infra/, which needs the alert email (or the plan would
  # show the email subscriptions being deleted). Read it from the live topic.
  if [ "${REPO#*/}" = "aws-optimization-agent" ]; then
    # shellcheck disable=SC2016 # the backticks are JMESPath literals, not shell
    alert_email=$(AWS_PROFILE="$ADMIN_PROFILE" aws sns list-subscriptions-by-topic --region eu-west-1 \
      --topic-arn "$(jq -r '.alert_topic_arns.value["eu-west-1"]' <<<"$outputs")" \
      --query 'Subscriptions[?Protocol==`email`].Endpoint | [0]' --output text)
    [ "$alert_email" = "None" ] && alert_email=""
    set_secret ALERT_EMAIL "$alert_email"
  fi
}

# Create or update a ruleset by name.
put_ruleset() { # name json
  local id
  id=$(gh api "repos/$REPO/rulesets" -q ".[] | select(.name==\"$1\") | .id")
  if [ -n "$id" ]; then
    gh api -X PUT "repos/$REPO/rulesets/$id" --input - --silent <<<"$2"
    echo "  updated ruleset '$1'"
  else
    gh api -X POST "repos/$REPO/rulesets" --input - --silent <<<"$2"
    echo "  created ruleset '$1'"
  fi
}

step_rulesets() {
  say "Rulesets on main"
  # 1. No bypass for anyone: changes reach main only through a PR whose
  #    `required` check (from GitHub Actions specifically) passed.
  put_ruleset "main: PR + checks (no bypass)" "$(cat <<EOF
{
  "name": "main: PR + checks (no bypass)",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false
    }},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": false,
      "required_status_checks": [{"context": "required", "integration_id": $GITHUB_ACTIONS_APP_ID}]
    }}
  ]
}
EOF
)"
  # 2. One approval from a code owner. Repo admins (you) may skip it, but only
  #    through a PR (bypass_mode pull_request), because GitHub doesn't let you
  #    approve your own PRs. The agent's GitHub App can never skip it.
  put_ruleset "main: code-owner approval" "$(cat <<EOF
{
  "name": "main: code-owner approval",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [{"actor_type": "RepositoryRole", "actor_id": $REPO_ADMIN_ROLE_ID, "bypass_mode": "pull_request"}],
  "rules": [
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 1,
      "dismiss_stale_reviews_on_push": true,
      "require_code_owner_review": true,
      "require_last_push_approval": true,
      "required_review_thread_resolution": false
    }}
  ]
}
EOF
)"
  gh api "repos/$REPO/rulesets" -q '.[] | "  \(.name): \(.enforcement)"'
}

for step in "$@"; do
  case "$step" in
    settings) step_settings ;;
    environment) step_environment ;;
    secrets) step_secrets ;;
    rulesets) step_rulesets ;;
    all) step_settings; step_environment; step_secrets; step_rulesets ;;
    *) echo "unknown step: $step" >&2; exit 2 ;;
  esac
done

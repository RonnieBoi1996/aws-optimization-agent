#!/usr/bin/env bash
# Destroy the sandbox stack (the M4 eval fixtures and everything else in sandbox/).
#
#   scripts/sandbox_destroy.sh
#
# For YOU to run, with your admin SSO session. Agents never run this, and it
# never touches infra/. Terraform lists what it will destroy and asks "yes"
# before doing anything.
#
# The code in sandbox/ still describes the fixtures, so the next merged PR that
# changes sandbox/ will create them again. To retire them for good, delete
# sandbox/fixtures.tf (and network.tf) in a PR instead.

set -euo pipefail

ACCOUNT="${OPTIMIZER_AWS_ACCOUNT_ID:?set OPTIMIZER_AWS_ACCOUNT_ID}"
REGION="${AWS_REGION:-eu-west-1}"
export AWS_PROFILE="${AWS_PROFILE:-AdministratorAccess-$ACCOUNT}"

cd "$(dirname "$0")/../sandbox"

# Same state location the CI pipeline uses (key = <stack dir>/terraform.tfstate).
terraform init -input=false -reconfigure \
  -backend-config="bucket=tfstate-$ACCOUNT-$REGION" \
  -backend-config="key=sandbox/terraform.tfstate" \
  -backend-config="region=$REGION" \
  -backend-config="use_lockfile=true" \
  -backend-config="encrypt=true"

terraform destroy -var="region=$REGION"

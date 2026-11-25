# infra/: bootstrap stack

This stack creates the trust and safety layer that everything else depends on.
**You apply it by hand, with your SSO admin profile.** CI can plan it but never
apply it: `tf-apply` is denied every write to `/optimizer/` IAM resources and
to this stack's state, so it can't widen its own permissions.

## What it creates

| Resource | Why |
| --- | --- |
| S3 bucket `tfstate-<account>-eu-west-1` | Terraform state for every stack. Versioned, SSE-S3, public access blocked, TLS-only, `prevent_destroy`. Locking uses S3 lock files (`use_lockfile`), no DynamoDB |
| GitHub OIDC provider | GitHub Actions gets short-lived AWS credentials, so no AWS keys are stored in GitHub |
| Role `optimizer-agent-read` | What the agents run as. Exact allow-list of read actions + deny-everything-else + data-plane denies + permission boundary. Assumable by your SSO admin role and the `optimizer-shadow` environment of this repo |
| Role `tf-plan` | `terraform plan` on PRs in both repos. `ReadOnlyAccess`, minus data-plane reads, plus state read and lock-file write |
| Role `tf-apply` | `terraform apply` from the `production` environment of both repos. `PowerUserAccess` + IAM only under `/workload/` with a required boundary. Denied: optimizer roles, detection, state/audit bucket config |
| Role `view-only` | Your daily browsing role (`ViewOnlyAccess`) |
| Policy `workload-boundary` | Every role created in infra-live must carry it. Blocks IAM writes, org/account changes, the state and audit buckets, and the detection resources |
| CloudTrail trail + bucket | Multi-region management events. EventBridge needs an active trail. Logs expire after 90 days |
| EventBridge → SNS, in eu-west-1 **and** us-east-1 | Emails you when `optimizer-agent-read`, `tf-plan` or `view-only` makes any write call (even a denied one). IAM/STS events arrive in us-east-1 |
| Compute Optimizer opt-in | Free tier. The Cost agent reads its recommendations |

Expected cost: roughly $0/month. The first trail's management events are free;
you pay only cents for S3 storage. EventBridge rules for AWS events, SNS email
and Compute Optimizer's free tier are free.

## Before you apply: check for things that already exist

```bash
export AWS_PROFILE=AdministratorAccess-<ACCOUNT_ID>
aws sso login
aws cloudtrail describe-trails --query 'trailList[].{Name:Name,MultiRegion:IsMultiRegionTrail}'
aws iam list-open-id-connect-providers
```

- If a **multi-region trail** already exists: `export TF_VAR_create_cloudtrail=false`
- If a `token.actions.githubusercontent.com` **OIDC provider** exists:
  `export TF_VAR_create_github_oidc_provider=false`

## Apply (once)

The state bucket is created by this stack, so the first apply uses local state,
and the state is then moved into the bucket.

```bash
cd infra
export AWS_PROFILE=AdministratorAccess-<ACCOUNT_ID>
export TF_VAR_alert_email="you@example.com"     # not committed anywhere

# 1. First apply with LOCAL state (backend_override.tf is git-ignored)
printf 'terraform {\n  backend "local" {}\n}\n' > backend_override.tf
terraform init
terraform plan -out=tfplan        # read it! expect ~40 resources to add
terraform apply tfplan

# 2. Move the state into the new bucket
terraform output -raw backend_hcl > backend.hcl  # git-ignored
rm backend_override.tf
terraform init -migrate-state -backend-config=backend.hcl   # answer "yes"
rm -f terraform.tfstate terraform.tfstate.backup tfplan

# 3. Confirm: this should show no changes, now reading state from S3
terraform plan
```

Then **click the confirmation link in both SNS emails** (one per region).
Alerts aren't delivered until you do.

## Set up your CLI profiles

```bash
terraform output -raw aws_config_profiles >> ~/.aws/config
```

This adds two profiles that assume roles *from* your SSO admin session:

```ini
[profile agent-dev]
source_profile = AdministratorAccess-<ACCOUNT_ID>
role_arn       = arn:aws:iam::<ACCOUNT_ID>:role/optimizer/optimizer-agent-read
region         = eu-west-1

[profile view-only]
source_profile = AdministratorAccess-<ACCOUNT_ID>
role_arn       = arn:aws:iam::<ACCOUNT_ID>:role/optimizer/view-only
region         = eu-west-1
```

Day to day: `AWS_PROFILE=agent-dev` for the optimizer, `AWS_PROFILE=view-only`
for browsing. Admin only for this stack and the sandbox. Your SSO session itself
is unchanged (12 hours). A role assumed *from* it lasts at most 1 hour (AWS limit
for role chaining), and the CLI refreshes it automatically while SSO is valid.

## Verify it works

```bash
# Who am I? -> .../assumed-role/optimizer-agent-read/...
AWS_PROFILE=agent-dev aws sts get-caller-identity

# Allowed: control-plane reads
AWS_PROFILE=agent-dev aws ec2 describe-instances --max-items 1
AWS_PROFILE=agent-dev aws s3api list-buckets --query 'Buckets[].Name'

# Denied: reading data, even the state file
AWS_PROFILE=agent-dev aws s3 cp s3://$(terraform output -raw state_bucket_name)/infra/terraform.tfstate -

# Denied AND alerted: write attempts. Expect an email within a few minutes for each.
AWS_PROFILE=agent-dev aws sns create-topic --name optimizer-detection-test          # eu-west-1 alert
AWS_PROFILE=agent-dev aws iam create-user --user-name optimizer-detection-test      # us-east-1 alert
```

## Rules for infra-live (the target repo)

Every IAM role created there must have:

```hcl
path                 = "/workload/"
permissions_boundary = "<workload_boundary_arn output>"
```

Otherwise `tf-apply` is denied. That's intentional: it's what stops an applied
change from creating a role more powerful than `tf-apply` itself.

## Changing this stack later

1. Open a PR. CI runs `plan` with `tf-plan` (from M2).
2. Merge after review.
3. Apply locally with your admin profile:
   `terraform init -backend-config=backend.hcl && terraform plan -out=tfplan && terraform apply tfplan`

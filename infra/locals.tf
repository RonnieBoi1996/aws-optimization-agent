data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  # Everything that defines the optimizer's own identity lives under this IAM
  # path. tf-apply and the workload boundary deny writes to it (rule 4).
  protected_path = "/optimizer/"
  # Roles/policies that infra-live creates must live under this path.
  workload_path = "/workload/"

  iam_arn_prefix = "arn:${local.partition}:iam::${local.account_id}"

  state_bucket_name      = "tfstate-${local.account_id}-${var.region}"
  cloudtrail_bucket_name = "cloudtrail-${local.account_id}-${var.region}"
  trail_name             = "optimizer-management-events"
  trail_arn              = "arn:${local.partition}:cloudtrail:${var.region}:${local.account_id}:trail/${local.trail_name}"

  # Predictable ARNs, so policies can reference roles without dependency cycles.
  role_arns = {
    agent_read = "${local.iam_arn_prefix}:role${local.protected_path}optimizer-agent-read"
    tf_plan    = "${local.iam_arn_prefix}:role${local.protected_path}tf-plan"
    tf_apply   = "${local.iam_arn_prefix}:role${local.protected_path}tf-apply"
    view_only  = "${local.iam_arn_prefix}:role${local.protected_path}view-only"
  }

  workload_boundary_arn = "${local.iam_arn_prefix}:policy${local.protected_path}workload-boundary"

  github_oidc_host = "token.actions.githubusercontent.com"
  # GitHub's immutable OIDC subject: "repo:<owner>@<owner_id>/<repo>@<repo_id>".
  # The numeric IDs never change, so a deleted-and-recreated repo with the same
  # name can't reuse these trust policies.
  agent_repo_sub = "repo:${var.github_owner}@${var.github_owner_id}/${var.agent_repo}@${var.agent_repo_id}"
  infra_repo_sub = "repo:${var.github_owner}@${var.github_owner_id}/${var.infra_repo}@${var.infra_repo_id}"

  # Data-plane reads: they return the *contents* of customer data, not its
  # configuration. Denied for the read-only roles even if a later policy allows them.
  data_plane_read_actions = [
    "athena:GetQueryResults",
    "dynamodb:BatchGetItem",
    "dynamodb:GetItem",
    "dynamodb:PartiQLSelect",
    "dynamodb:Query",
    "dynamodb:Scan",
    "ec2:GetPasswordData",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
    "kinesis:GetRecords",
    "kms:Decrypt",
    "kms:GenerateDataKey*",
    "lambda:GetFunction", # returns a download URL for the function's code
    "logs:FilterLogEvents",
    "logs:GetLogEvents",
    "logs:StartQuery",
    "rds-data:ExecuteStatement",
    "s3:GetObject",
    "s3:GetObjectVersion",
    "secretsmanager:BatchGetSecretValue",
    "secretsmanager:GetSecretValue",
    "sqs:ReceiveMessage",
    "ssm:GetParameter",
    "ssm:GetParameterHistory",
    "ssm:GetParameters",
    "ssm:GetParametersByPath",
  ]
}

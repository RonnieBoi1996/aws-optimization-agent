# Permission boundaries cap what a role can EVER do, whatever policies get
# attached to it later. Effective permissions = identity policies ∩ boundary.

# --- Boundary for optimizer-agent-read: read-only, no data-plane reads --------

locals {
  # Services the agent's tools read from. The boundary lets them use
  # Describe*/List*/Get*; the agent's own policy narrows that to exact actions.
  agent_read_services = [
    "access-analyzer", "autoscaling", "ce", "cloudwatch", "compute-optimizer",
    "ec2", "iam", "lambda", "pi", "pricing", "rds", "s3", "tag",
  ]
}

data "aws_iam_policy_document" "agent_boundary" {
  statement {
    sid       = "ReadOnlyVerbsOnAgentServices"
    actions   = flatten([for s in local.agent_read_services : ["${s}:Describe*", "${s}:List*", "${s}:Get*"]])
    resources = ["*"]
  }

  statement {
    # Read-only APIs whose names don't start with Describe/List/Get.
    sid       = "OtherReadOnlyApis"
    actions   = ["access-analyzer:ValidatePolicy"]
    resources = ["*"]
  }

  statement {
    sid       = "DenyDataPlaneReads"
    effect    = "Deny"
    actions   = local.data_plane_read_actions
    resources = ["*"]
  }
}

resource "aws_iam_policy" "agent_boundary" {
  name        = "optimizer-agent-read-boundary"
  path        = local.protected_path
  description = "Caps optimizer-agent-read to read-only control-plane APIs."
  policy      = data.aws_iam_policy_document.agent_boundary.json
}

# --- Boundary for roles that tf-apply creates in infra-live ------------------
# tf-apply may only create IAM roles that carry THIS boundary, so a role it
# creates can never be more powerful than this, and can never touch the
# optimizer's own identity, the alerts, or the state bucket.

data "aws_iam_policy_document" "workload_boundary" {
  # checkov:skip=CKV_AWS_107:A boundary is a ceiling, not a grant. Workload roles still need their own narrow policies.
  # checkov:skip=CKV_AWS_108:Same: this caps workload roles; it grants nothing by itself.
  # checkov:skip=CKV_AWS_109:Same: permissions-management limits are enforced by the Deny statements below.
  # checkov:skip=CKV_AWS_110:Same: privilege escalation is blocked by DenyIamWrites below.
  # checkov:skip=CKV_AWS_111:Same: write limits are enforced by the Deny statements below.
  # checkov:skip=CKV_AWS_356:A boundary must allow "*" so it doesn't silently break workloads; the deny list does the protecting.
  # checkov:skip=CKV_AWS_1:Boundary, not a grant: it only caps workload roles, which still need their own narrow identity policies.
  # checkov:skip=CKV_AWS_49:Same as CKV_AWS_1: "*" here is the ceiling; the Deny statements below remove IAM, org, state and detection access.
  statement {
    sid       = "AllowUpToWhatIdentityPoliciesGrant"
    actions   = ["*"]
    resources = ["*"]
  }

  statement {
    sid         = "DenyIamWrites"
    effect      = "Deny"
    not_actions = ["iam:Get*", "iam:List*", "iam:PassRole"]
    resources   = ["arn:${local.partition}:iam::*:*"]
  }

  statement {
    sid       = "DenyAccountAndOrgChanges"
    effect    = "Deny"
    actions   = ["organizations:*", "account:*"]
    resources = ["*"]
  }

  statement {
    sid       = "DenyAssumingOptimizerRoles"
    effect    = "Deny"
    actions   = ["sts:AssumeRole"]
    resources = ["${local.iam_arn_prefix}:role${local.protected_path}*"]
  }

  statement {
    sid     = "DenyTouchingStateAndAuditBuckets"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      "arn:${local.partition}:s3:::${local.state_bucket_name}",
      "arn:${local.partition}:s3:::${local.state_bucket_name}/*",
      "arn:${local.partition}:s3:::${local.cloudtrail_bucket_name}",
      "arn:${local.partition}:s3:::${local.cloudtrail_bucket_name}/*",
    ]
  }

  statement {
    sid       = "DenyTamperingWithDetection"
    effect    = "Deny"
    actions   = local.detection_tamper_actions
    resources = local.detection_resource_arns
  }
}

resource "aws_iam_policy" "workload_boundary" {
  name        = "workload-boundary"
  path        = local.protected_path
  description = "Required permission boundary for every role tf-apply creates."
  policy      = data.aws_iam_policy_document.workload_boundary.json
}

locals {
  # Actions that would blind or silence the write-call alert.
  detection_tamper_actions = [
    "cloudtrail:DeleteTrail",
    "cloudtrail:PutEventSelectors",
    "cloudtrail:StopLogging",
    "cloudtrail:UpdateTrail",
    "events:DeleteRule",
    "events:DisableRule",
    "events:PutRule",
    "events:PutTargets",
    "events:RemoveTargets",
    "sns:DeleteTopic",
    "sns:SetTopicAttributes",
    "sns:Subscribe",
  ]
  detection_resource_arns = [
    local.trail_arn,
    "arn:${local.partition}:events:*:${local.account_id}:rule/optimizer-*",
    "arn:${local.partition}:sns:*:${local.account_id}:optimizer-*",
  ]
}

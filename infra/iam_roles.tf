# The four roles from section 2. All live under /optimizer/, which tf-apply and
# the workload boundary are not allowed to modify.

# Trust: only the IAM role behind your SSO admin permission set may assume.
data "aws_iam_policy_document" "sso_admin_trust" {
  statement {
    sid     = "SsoAdminOnly"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
    # "root" alone would let ANY principal in the account with sts:AssumeRole in;
    # this condition narrows it to the SSO admin role.
    condition {
      test     = "ArnLike"
      variable = "aws:PrincipalArn"
      values   = ["${local.iam_arn_prefix}:role/aws-reserved/sso.amazonaws.com/*${var.sso_admin_role_pattern}"]
    }
  }
}

# =============================================================================
# optimizer-agent-read: what the agents run as. Read-only, three layers deep.
# =============================================================================

locals {
  # EXACT actions the aws-insights tools call. Grouped by agent; the tool code
  # lands in M3 (cost), M6 (IAM) and M8 (latency), and this list is revisited then.
  agent_allowed_actions = sort(distinct(concat(
    [
      # load_context: map resources to services via tags
      "tag:GetResources",
    ],
    [
      # Cost agent
      "autoscaling:DescribeAutoScalingGroups",
      "ce:GetCostAndUsage", # billed per request; the server caches responses
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
      "compute-optimizer:GetEBSVolumeRecommendations",
      "compute-optimizer:GetEC2InstanceRecommendations",
      "compute-optimizer:GetEnrollmentStatus",
      "compute-optimizer:GetLambdaFunctionRecommendations",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeInstances",
      "ec2:DescribeVolumes",
      "pricing:GetProducts",
      "s3:GetBucketLocation",
      "s3:GetBucketTagging",
      "s3:GetLifecycleConfiguration",
      "s3:ListAllMyBuckets",
    ],
    [
      # IAM agent
      "access-analyzer:GetFindingV2",
      "access-analyzer:ListAnalyzers",
      "access-analyzer:ListFindingsV2",
      "access-analyzer:ValidatePolicy",
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
      "iam:ListRoles",
    ],
    [
      # Latency agent. Lambda cold starts are not covered yet: the obvious source
      # (Logs Insights) reads log contents, which rule 7 forbids. Decided in M8.
      "lambda:GetFunctionConfiguration",
      "lambda:ListFunctions",
      "pi:DescribeDimensionKeys",
      "pi:GetResourceMetrics",
      "rds:DescribeDBInstances",
    ],
  )))
}

data "aws_iam_policy_document" "agent_read" {
  statement {
    sid       = "AllowListedReadActions"
    actions   = local.agent_allowed_actions
    resources = ["*"]
  }

  # Layer 2a: deny everything NOT on the allow-list. This covers all IAM and
  # resource writes, even if someone attaches a broad policy to this role later.
  statement {
    sid         = "DenyEverythingElse"
    effect      = "Deny"
    not_actions = local.agent_allowed_actions
    resources   = ["*"]
  }

  # Layer 2b: data-plane reads, denied explicitly by name so the intent survives
  # even if the allow-list above grows carelessly.
  statement {
    sid       = "DenyDataPlaneReads"
    effect    = "Deny"
    actions   = local.data_plane_read_actions
    resources = ["*"]
  }
}

resource "aws_iam_role" "agent_read" {
  name                 = "optimizer-agent-read"
  path                 = local.protected_path
  description          = "Read-only role for the optimizer agents (local runs and scheduled CI runs)."
  max_session_duration = 3600
  permissions_boundary = aws_iam_policy.agent_boundary.arn # layer 3
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      jsondecode(data.aws_iam_policy_document.sso_admin_trust.json).Statement,
      jsondecode(data.aws_iam_policy_document.github_trust["agent_read"].json).Statement,
    )
  })
}

resource "aws_iam_role_policy" "agent_read" {
  name   = "optimizer-agent-read"
  role   = aws_iam_role.agent_read.id
  policy = data.aws_iam_policy_document.agent_read.json
}

# =============================================================================
# tf-plan: GitHub Actions on pull_request. Reads for planning + lock file only.
# =============================================================================

data "aws_iam_policy_document" "tf_plan" {
  statement {
    sid       = "ListStateBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.state.arn]
  }

  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }

  statement {
    # S3-native locking writes and deletes "<key>.tflock" next to the state file.
    sid       = "ReadWriteLockFileOnly"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/*.tflock"]
  }

  statement {
    # Same data-plane denies as the agent, except reading state objects.
    sid       = "DenyDataPlaneReads"
    effect    = "Deny"
    actions   = [for a in local.data_plane_read_actions : a if !startswith(a, "s3:GetObject")]
    resources = ["*"]
  }

  statement {
    sid           = "DenyObjectReadsOutsideState"
    effect        = "Deny"
    actions       = ["s3:GetObject", "s3:GetObjectVersion"]
    not_resources = ["${aws_s3_bucket.state.arn}/*"]
  }
}

resource "aws_iam_role" "tf_plan" {
  name                 = "tf-plan"
  path                 = local.protected_path
  description          = "terraform plan on pull requests (GitHub Actions OIDC)."
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.github_trust["tf_plan"].json
}

# Refreshing state needs to read every kind of resource a stack might contain,
# so the AWS-managed ReadOnlyAccess is used here. The data-plane denies above
# take away the parts of it that read actual data.
resource "aws_iam_role_policy_attachment" "tf_plan_readonly" {
  role       = aws_iam_role.tf_plan.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy" "tf_plan" {
  name   = "tf-plan"
  role   = aws_iam_role.tf_plan.id
  policy = data.aws_iam_policy_document.tf_plan.json
}

# =============================================================================
# tf-apply: GitHub Actions on push to main, `production` environment only.
# Applies sandbox/ and infra-live. It can NOT apply infra/ (this stack): it is
# denied every write to /optimizer/, so it can never widen its own permissions.
# infra/ is applied by you, with your admin profile.
# =============================================================================

data "aws_iam_policy_document" "tf_apply" {
  # checkov:skip=CKV_AWS_356:iam:Get*/List* need "*" to refresh state for any resource.
  statement {
    sid       = "IamReadForRefresh"
    actions   = ["iam:Get*", "iam:List*"]
    resources = ["*"]
  }

  statement {
    # Creating or changing a role's permissions is only allowed when the role
    # carries the workload boundary. That's what stops privilege escalation.
    sid = "ManageWorkloadRolesWithBoundary"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreateRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:PutRolePermissionsBoundary",
      "iam:PutRolePolicy",
    ]
    resources = ["${local.iam_arn_prefix}:role${local.workload_path}*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.workload_boundary_arn]
    }
  }

  statement {
    # These actions don't support the boundary condition key; the path limits them.
    sid = "ManageWorkloadRolesOther"
    actions = [
      "iam:DeleteRole",
      "iam:PassRole",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:UpdateRole",
      "iam:UpdateRoleDescription",
    ]
    resources = ["${local.iam_arn_prefix}:role${local.workload_path}*"]
  }

  statement {
    sid = "ManageWorkloadPoliciesAndInstanceProfiles"
    actions = [
      "iam:AddRoleToInstanceProfile",
      "iam:CreateInstanceProfile",
      "iam:CreatePolicy",
      "iam:CreatePolicyVersion",
      "iam:DeleteInstanceProfile",
      "iam:DeletePolicy",
      "iam:DeletePolicyVersion",
      "iam:RemoveRoleFromInstanceProfile",
      "iam:SetDefaultPolicyVersion",
      "iam:TagInstanceProfile",
      "iam:TagPolicy",
      "iam:UntagInstanceProfile",
      "iam:UntagPolicy",
    ]
    resources = [
      "${local.iam_arn_prefix}:policy${local.workload_path}*",
      "${local.iam_arn_prefix}:instance-profile${local.workload_path}*",
    ]
  }

  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["${local.iam_arn_prefix}:role/aws-service-role/*"]
  }

  # ---- explicit denies: these win over PowerUserAccess and everything above ----

  statement {
    sid         = "DenyChangesToOptimizerIdentity"
    effect      = "Deny"
    not_actions = ["iam:Get*", "iam:List*"]
    resources = [
      "${local.iam_arn_prefix}:role${local.protected_path}*",
      "${local.iam_arn_prefix}:policy${local.protected_path}*",
      "${local.iam_arn_prefix}:oidc-provider/${local.github_oidc_host}",
    ]
  }

  statement {
    sid       = "DenyRemovingBoundaries"
    effect    = "Deny"
    actions   = ["iam:DeleteRolePermissionsBoundary", "iam:DeleteUserPermissionsBoundary"]
    resources = ["*"]
  }

  statement {
    sid       = "DenyTamperingWithDetection"
    effect    = "Deny"
    actions   = local.detection_tamper_actions
    resources = local.detection_resource_arns
  }

  statement {
    sid    = "DenyReconfiguringStateAndAuditBuckets"
    effect = "Deny"
    actions = [
      "s3:DeleteBucket",
      "s3:DeleteBucketPolicy",
      "s3:PutBucketOwnershipControls",
      "s3:PutBucketPolicy",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "s3:PutLifecycleConfiguration",
    ]
    resources = [
      aws_s3_bucket.state.arn,
      "arn:${local.partition}:s3:::${local.cloudtrail_bucket_name}",
    ]
  }

  statement {
    sid       = "DenyDeletingStateHistoryAndAuditLogs"
    effect    = "Deny"
    actions   = ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObject"]
    resources = ["arn:${local.partition}:s3:::${local.cloudtrail_bucket_name}/*"]
  }

  statement {
    sid       = "DenyDeletingStateVersions"
    effect    = "Deny"
    actions   = ["s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.state.arn}/*"]
  }

  statement {
    # infra/ is applied by a human only; CI must not rewrite its state either.
    sid       = "DenyWritingBootstrapState"
    effect    = "Deny"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.state.arn}/infra/*"]
  }
}

resource "aws_iam_role" "tf_apply" {
  name                 = "tf-apply"
  path                 = local.protected_path
  description          = "terraform apply on push to main, production environment (GitHub Actions OIDC)."
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.github_trust["tf_apply"].json
}

# PowerUserAccess = everything except IAM, Organizations and Account management.
# IAM is then granted back narrowly (above) for /workload/ roles only.
resource "aws_iam_role_policy_attachment" "tf_apply_poweruser" {
  role       = aws_iam_role.tf_apply.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role_policy" "tf_apply" {
  name   = "tf-apply"
  role   = aws_iam_role.tf_apply.id
  policy = data.aws_iam_policy_document.tf_apply.json
}

# =============================================================================
# view-only: you, for day-to-day console/CLI browsing instead of admin.
# =============================================================================

resource "aws_iam_role" "view_only" {
  name                 = "view-only"
  path                 = local.protected_path
  description          = "Day-to-day browsing (AWS-managed ViewOnlyAccess)."
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.sso_admin_trust.json
}

resource "aws_iam_role_policy_attachment" "view_only" {
  role       = aws_iam_role.view_only.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/job-function/ViewOnlyAccess"
}

# Lets GitHub Actions exchange its short-lived OIDC token for AWS credentials,
# so no AWS keys are ever stored in GitHub.

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url            = "https://${local.github_oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # No thumbprint_list: AWS validates GitHub's certificate against its own trusted CAs.
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 0 : 1
  url   = "https://${local.github_oidc_host}"
}

locals {
  github_oidc_provider_arn = (var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
  : data.aws_iam_openid_connect_provider.github[0].arn)
}

# Builds a trust policy for GitHub Actions. Values are exact "sub" claims, e.g.
# "repo:owner@123/name@456:pull_request". Never wildcards.
data "aws_iam_policy_document" "github_trust" {
  for_each = {
    agent_read = ["${local.agent_repo_sub}:environment:optimizer-shadow"]
    tf_plan = [
      "${local.agent_repo_sub}:pull_request",
      "${local.infra_repo_sub}:pull_request",
    ]
    tf_apply = [
      "${local.agent_repo_sub}:environment:production",
      "${local.infra_repo_sub}:environment:production",
    ]
  }

  statement {
    sid     = "GitHubActionsOIDC"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.github_oidc_host}:sub"
      values   = each.value
    }
  }
}

# Nothing account-specific has a default here: this repo is public.
# The account ID is read at plan time from data.aws_caller_identity.

variable "region" {
  description = "Home region for the state bucket, CloudTrail bucket and alerts."
  type        = string
  default     = "eu-west-1"
}

variable "detection_regions" {
  description = <<-EOT
    Regions where the "write call by a read-only role" alert runs. Global services
    such as IAM deliver their CloudTrail events in us-east-1, so keep it in the list.
  EOT
  type        = list(string)
  default     = ["eu-west-1", "us-east-1"]
}

variable "github_owner" {
  description = "GitHub user or org that owns both repos."
  type        = string
  default     = "RonnieBoi1996"
}

variable "agent_repo" {
  description = "This repo (agent code + infra/ + sandbox/)."
  type        = string
  default     = "aws-optimization-agent"
}

variable "infra_repo" {
  description = "Target infrastructure repo the agent opens PRs against."
  type        = string
  default     = "infra-live"
}

variable "sso_admin_role_pattern" {
  description = <<-EOT
    Name pattern of the IAM role behind your SSO admin permission set. Only that
    role may assume optimizer-agent-read and view-only from your laptop.
  EOT
  type        = string
  default     = "AWSReservedSSO_AdministratorAccess_*"
}

variable "alert_email" {
  description = <<-EOT
    Email for write-call alerts. Pass it with TF_VAR_alert_email instead of
    committing it. null = create the topics but no subscription.
  EOT
  type        = string
  default     = null
}

variable "create_cloudtrail" {
  description = <<-EOT
    Create a multi-region CloudTrail trail. EventBridge only receives
    "AWS API Call via CloudTrail" events while a trail is logging. Set to false
    if the account already has a multi-region trail with management events.
  EOT
  type        = bool
  default     = true
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Create the GitHub Actions OIDC provider. An account can only have one per URL,
    so set to false if it already exists (it is then looked up instead).
  EOT
  type        = bool
  default     = true
}

variable "enable_compute_optimizer" {
  description = <<-EOT
    Opt the account in to Compute Optimizer (free tier). The Cost agent's
    rightsizing tool reads its recommendations. Opting in is a write action,
    which is why it lives here and not in the read-only agent.
  EOT
  type        = bool
  default     = true
}

# These outputs contain the account ID. They print to your terminal only;
# never paste them into a committed file.

output "state_bucket_name" {
  value = aws_s3_bucket.state.id
}

output "role_arns" {
  description = "ARNs of the four roles (GitHub secrets in M2 use these)."
  value = {
    optimizer_agent_read = aws_iam_role.agent_read.arn
    tf_plan              = aws_iam_role.tf_plan.arn
    tf_apply             = aws_iam_role.tf_apply.arn
    view_only            = aws_iam_role.view_only.arn
  }
}

output "workload_boundary_arn" {
  description = "Every IAM role created in infra-live must set permissions_boundary to this."
  value       = aws_iam_policy.workload_boundary.arn
}

output "alert_topic_arns" {
  value = { for region, m in module.detection : region => m.topic_arn }
}

# `terraform output -raw backend_hcl > backend.hcl` (backend.hcl is git-ignored)
output "backend_hcl" {
  value = <<-EOT
    bucket       = "${aws_s3_bucket.state.id}"
    key          = "infra/terraform.tfstate"
    region       = "${var.region}"
    use_lockfile = true
    encrypt      = true
  EOT
}

# `terraform output -raw aws_config_profiles >> ~/.aws/config`
output "aws_config_profiles" {
  value = <<-EOT

    [profile agent-dev]
    source_profile = AdministratorAccess-${local.account_id}
    role_arn       = ${aws_iam_role.agent_read.arn}
    region         = ${var.region}

    [profile view-only]
    source_profile = AdministratorAccess-${local.account_id}
    role_arn       = ${aws_iam_role.view_only.arn}
    region         = ${var.region}
  EOT
}

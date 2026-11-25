# Alert on any write API call (readOnly == false) made by the roles that should
# only ever read. One copy per region in var.detection_regions, because
# EventBridge only sees CloudTrail events from its own region (IAM/STS: us-east-1).
#
# Denied calls are recorded too, so even a blocked write attempt sends an email.

module "detection" {
  source   = "./modules/detection"
  for_each = toset(var.detection_regions)

  region      = each.value
  name_prefix = "optimizer"
  alert_email = var.alert_email
  watched_role_arns = [
    local.role_arns.agent_read,
    local.role_arns.tf_plan,
    local.role_arns.view_only,
  ]
}

# Compute Optimizer opt-in (free tier). Recommendations start appearing after
# roughly a day of metrics.
resource "aws_computeoptimizer_enrollment_status" "this" {
  count  = var.enable_compute_optimizer ? 1 : 0
  status = "Active"
}

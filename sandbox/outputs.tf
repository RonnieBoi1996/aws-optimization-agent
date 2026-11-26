output "resource_group_arn" {
  value = aws_resourcegroups_group.sandbox.arn
}

output "fixture_instance_ids" {
  description = "M4 eval fixtures: Name tag -> instance ID."
  value       = { for k, i in aws_instance.fixture : i.tags["Name"] => i.id }
}

output "fixture_volume_ids" {
  description = "M4 eval fixtures: Name tag -> volume ID."
  value       = { for k, v in aws_ebs_volume.fixture : v.tags["Name"] => v.id }
}

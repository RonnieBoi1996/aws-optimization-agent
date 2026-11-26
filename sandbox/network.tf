# A tiny private network for the M4 fixtures. No internet gateway, no NAT, no
# inbound or outbound rules: the instances can't reach anything and nothing can
# reach them. All free. CloudWatch's basic EC2 metrics (CPU, network) still work,
# because AWS collects them from the hypervisor, not from inside the instance.

locals {
  # One fixed zone: the volumes must sit in the same AZ as the subnet, and a
  # fixed name keeps the plan stable.
  availability_zone = "${var.region}a"
}

resource "aws_vpc" "sandbox" {
  cidr_block = "10.42.0.0/24"
  tags       = { Name = "sandbox-vpc" }
  # checkov:skip=CKV2_AWS_11:Flow logs cost money and there is no traffic to log (no IGW, no rules).
}

resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.sandbox.id
  cidr_block              = "10.42.0.0/26"
  availability_zone       = local.availability_zone
  map_public_ip_on_launch = false
  tags                    = { Name = "sandbox-private" }
}

# Take over the VPC's default security group and leave it with no rules, so
# nothing can accidentally use its allow-all defaults.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.sandbox.id
}

resource "aws_security_group" "fixtures" {
  name        = "sandbox-fixtures"
  description = "Optimizer sandbox fixtures. No ingress, no egress."
  vpc_id      = aws_vpc.sandbox.id
  # No rules on purpose. Terraform removes AWS's default allow-all egress rule.
}

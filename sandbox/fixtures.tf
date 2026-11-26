# M4 eval fixtures: known problems the Cost agent should (or must not) find.
# The ground truth is in evals/cases.yaml, matched by the `eval_case` tag.
#
# Names are deliberately neutral ("sandbox-web-1", not "idle-instance"): the
# agent sees Name tags, and a name that gives away the answer would make the
# eval meaningless. The cost tools never pass `eval_case` to the model.
#
# Cost while running: about $1.30/day in total (see sandbox/README.md). Destroy
# with scripts/sandbox_destroy.sh when you're done.

# Amazon Linux 2023, newest at first apply. ignore_changes below stops every
# new AMI release from replacing the instances (which would also reset their
# CloudWatch history and Compute Optimizer findings).
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

locals {
  instances = {
    # Case idle-ec2: no workload at all. Should be found by get_idle_ec2.
    web = { name = "sandbox-web-1", type = "t3.nano", case = "idle-ec2", service = "sandbox", load = false }
    # Case oversized-ec2: light steady load on a bigger-than-needed type.
    # Should be found by get_rightsizing_recommendations (Compute Optimizer).
    worker = { name = "sandbox-worker-1", type = "t3.small", case = "oversized-ec2", service = "sandbox", load = true }
    # Case postmortem-ec2: same shape as the worker, but its service's docs hold
    # a post-mortem saying not to downsize it (context/sandbox-reports/docs/).
    reports = { name = "sandbox-report-builder", type = "t3.small", case = "postmortem-ec2", service = "sandbox-reports", load = true }
  }

  volumes = {
    # Case unattached-ebs: a volume attached to nothing. Should be proposed.
    data = { name = "sandbox-data-1", case = "unattached-ebs" }
    # Case do-not-touch-ebs: also unattached, but listed in
    # context/sandbox/constraints.yaml do_not_touch (by Name tag). Must not be proposed.
    archive = { name = "sandbox-archive-vol", case = "do-not-touch-ebs" }
  }
}

resource "aws_instance" "fixture" {
  for_each = local.instances

  ami                         = data.aws_ami.al2023.id
  instance_type               = each.value.type
  subnet_id                   = aws_subnet.private.id
  vpc_security_group_ids      = [aws_security_group.fixtures.id]
  associate_public_ip_address = false
  ebs_optimized               = true
  user_data                   = each.value.load ? file("${path.module}/files/load.sh") : null

  # "standard" = no surprise charges: a burstable instance that runs out of CPU
  # credits slows down instead of billing for extra CPU ("unlimited", the default).
  credit_specification {
    cpu_credits = "standard"
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  tags = {
    Name      = each.value.name
    service   = each.value.service
    eval_case = each.value.case
  }

  lifecycle {
    ignore_changes = [ami]
  }

  # checkov:skip=CKV_AWS_126:Detailed monitoring costs ~$2/instance/month; the eval uses basic 5-minute metrics.
  # checkov:skip=CKV2_AWS_41:No instance role on purpose: the fixtures call no AWS APIs and have no network.
}

resource "aws_ebs_volume" "fixture" {
  for_each = local.volumes

  availability_zone = aws_subnet.private.availability_zone
  size              = 1
  type              = "gp3"
  encrypted         = true

  tags = {
    Name      = each.value.name
    eval_case = each.value.case
  }

  # checkov:skip=CKV_AWS_189:A customer-managed KMS key costs $1/month; the AWS-managed key is enough for 1 GB of nothing.
}

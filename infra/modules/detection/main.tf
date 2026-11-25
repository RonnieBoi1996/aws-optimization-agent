# EventBridge rule -> SNS topic (-> email), all in one region.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "region" {
  description = "Region to deploy into (AWS provider 6.x per-resource region)."
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names. tf-apply is denied changes to optimizer-* rules and topics."
  type        = string
}

variable "watched_role_arns" {
  description = "Role ARNs that must never make a write call."
  type        = list(string)
}

variable "alert_email" {
  description = "Email subscriber, or null for none."
  type        = string
  default     = null
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_sns_topic" "alerts" {
  # checkov:skip=CKV_AWS_26:EventBridge can't publish to a topic encrypted with the AWS-managed SNS key, and a CMK isn't worth it for alert metadata (no customer data).
  region = var.region
  name   = "${var.name_prefix}-readonly-role-writes"
}

resource "aws_cloudwatch_event_rule" "writes" {
  region      = var.region
  name        = "${var.name_prefix}-readonly-role-writes"
  description = "Write API call (readOnly=false) by a role that must only read."

  # Matches on the role behind the session (sessionIssuer), not the session name,
  # so it can't be dodged by picking a different session name.
  event_pattern = jsonencode({
    "detail-type" = ["AWS API Call via CloudTrail"]
    detail = {
      readOnly = [false]
      userIdentity = {
        sessionContext = {
          sessionIssuer = {
            arn = var.watched_role_arns
          }
        }
      }
    }
  })
}

resource "aws_cloudwatch_event_target" "sns" {
  region    = var.region
  rule      = aws_cloudwatch_event_rule.writes.name
  target_id = "sns"
  arn       = aws_sns_topic.alerts.arn

  # A readable one-line email instead of a raw JSON event.
  input_transformer {
    input_paths = {
      role   = "$.detail.userIdentity.sessionContext.sessionIssuer.userName"
      event  = "$.detail.eventName"
      source = "$.detail.eventSource"
      region = "$.detail.awsRegion"
      time   = "$.detail.eventTime"
      error  = "$.detail.errorCode"
      id     = "$.detail.eventID"
    }
    input_template = "\"ALERT: read-only role <role> made a WRITE call <event> (<source>) in <region> at <time>. Error code: <error>. CloudTrail event ID: <id>\""
  }
}

data "aws_iam_policy_document" "topic" {
  statement {
    sid       = "AllowThisRuleToPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.writes.arn]
    }
  }

  statement {
    sid    = "OwnerManagesTopic"
    effect = "Allow"
    actions = [
      "sns:GetTopicAttributes", "sns:SetTopicAttributes", "sns:Subscribe",
      "sns:ListSubscriptionsByTopic", "sns:Publish", "sns:DeleteTopic",
    ]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  region = var.region
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.topic.json
}

resource "aws_sns_topic_subscription" "email" {
  count = var.alert_email == null ? 0 : 1

  region    = var.region
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

output "topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "rule_arn" {
  value = aws_cloudwatch_event_rule.writes.arn
}

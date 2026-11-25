# A multi-region trail. Needed because EventBridge only receives
# "AWS API Call via CloudTrail" events while a trail is logging. The first trail
# that records management events is free (you only pay for S3 storage).

resource "aws_s3_bucket" "cloudtrail" {
  # checkov:skip=CKV_AWS_18:Access logging on the audit bucket would need yet another bucket; not worth it for personal infra.
  # checkov:skip=CKV_AWS_144:Cross-region replication not needed for short-lived personal infra.
  # checkov:skip=CKV_AWS_145:SSE-S3 keeps the setup simple; a KMS CMK adds cost and key-policy management.
  # checkov:skip=CKV2_AWS_62:No consumer for S3 event notifications; alerts come from EventBridge.
  # checkov:skip=CKV_AWS_21:Log objects are write-once and expire after 90 days; tf-apply is denied deleting them.
  count  = var.create_cloudtrail ? 1 : 0
  bucket = local.cloudtrail_bucket_name
}

resource "aws_s3_bucket_server_side_encryption_configuration" "cloudtrail" {
  count  = var.create_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  count                   = var.create_cloudtrail ? 1 : 0
  bucket                  = aws_s3_bucket.cloudtrail[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "cloudtrail" {
  count  = var.create_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  count  = var.create_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id

  rule {
    id     = "expire-logs"
    status = "Enabled"
    filter {}

    # CloudTrail's own 90-day Event History covers investigations; old files only cost money.
    expiration {
      days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "cloudtrail_bucket" {
  count = var.create_cloudtrail ? 1 : 0

  statement {
    sid       = "CloudTrailAclCheck"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.cloudtrail[0].arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  statement {
    sid       = "CloudTrailWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.cloudtrail[0].arn}/AWSLogs/${local.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.cloudtrail[0].arn,
      "${aws_s3_bucket.cloudtrail[0].arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  count  = var.create_cloudtrail ? 1 : 0
  bucket = aws_s3_bucket.cloudtrail[0].id
  policy = data.aws_iam_policy_document.cloudtrail_bucket[0].json

  depends_on = [aws_s3_bucket_public_access_block.cloudtrail]
}

resource "aws_cloudtrail" "main" {
  # checkov:skip=CKV_AWS_35:SSE-S3 on the bucket instead of a KMS CMK (cost and key management).
  # checkov:skip=CKV_AWS_252:Alerts are driven by EventBridge, not by per-file SNS notifications.
  # checkov:skip=CKV2_AWS_10:EventBridge already receives the events; a CloudWatch Logs copy would only add cost.
  count = var.create_cloudtrail ? 1 : 0

  name                          = local.trail_name
  s3_bucket_name                = aws_s3_bucket.cloudtrail[0].id
  is_multi_region_trail         = true
  include_global_service_events = true # IAM, STS etc. (delivered in us-east-1)
  enable_log_file_validation    = true # detect edited/deleted log files

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}

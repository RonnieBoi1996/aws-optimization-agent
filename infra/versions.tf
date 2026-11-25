terraform {
  # 1.10+ is required for S3-native state locking (use_lockfile).
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial configuration: bucket/key/region are passed at init time with
  # -backend-config=backend.hcl (git-ignored), because the bucket name contains
  # the account ID and this repo is public. See infra/README.md.
  backend "s3" {}
}

terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # Partial config: CI passes bucket/key/region with -backend-config.
  # State key: sandbox/terraform.tfstate (a dedicated key isolates the sandbox).
  backend "s3" {}
}

provider "aws" {
  region = var.region

  # There is no separate sandbox account, so the sandbox is isolated by tags:
  # everything here carries environment = sandbox, and the resource group below
  # lists exactly those resources.
  default_tags {
    tags = {
      project     = "aws-optimizer-sandbox"
      environment = "sandbox"
      managed_by  = "terraform"
      # How the agents know a resource is managed by Terraform, and where
      # (context/README.md). Without these it's reported as unmanaged.
      iac_repo  = "aws-optimization-agent"
      iac_stack = "sandbox"
      service   = "sandbox"
    }
  }
}

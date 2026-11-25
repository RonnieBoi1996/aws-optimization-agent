provider "aws" {
  region = var.region

  # Tags on every resource this stack creates, so it's obvious in the console
  # what belongs to the optimizer bootstrap.
  default_tags {
    tags = {
      project    = "aws-optimizer"
      stack      = "infra"
      managed_by = "terraform"
    }
  }
}

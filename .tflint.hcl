# tflint config shared by every Terraform stack in this repo (infra/, sandbox/).
# Run from a stack directory: tflint --init && tflint --config ../.tflint.hcl

config {
  call_module_type = "local" # also lint ./modules/*
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.50.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}

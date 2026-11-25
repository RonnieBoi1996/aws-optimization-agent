# pipelines/: the Terraform pipeline for infra-live

Copy-ready version of this repo's pipeline for the target infrastructure repo
(`infra-live`), the repo the agent opens PRs against.

## Layout expected in infra-live

```text
infra-live/
  .github/workflows/terraform-pr.yml      <- pipelines/workflows/terraform-pr.yml
  .github/workflows/terraform-apply.yml   <- pipelines/workflows/terraform-apply.yml
  .github/CODEOWNERS                      <- "* @RonnieBoi1996"
  ci/                                     <- this repo's ci/ folder, unchanged
  .tflint.hcl                             <- this repo's .tflint.hcl, unchanged
  .optimizer/manifests/                   <- written by the agent, one JSON per proposal
  modules/<name>/                         <- shared modules (a change here plans every stack)
  stacks/<name>/                          <- one directory per Terraform stack
    versions.tf                           <- must contain: backend "s3" {}
```

- **State keys:** `infra-live/stacks/<name>/terraform.tfstate`, in the same bucket
  as this repo.
- **Plans** use `tf-plan` (its OIDC trust includes `infra-live:pull_request`).
- **Applies** use `tf-apply` (`infra-live:environment:production`).
- **IAM roles** created in infra-live must use `path = "/workload/"` and
  `permissions_boundary` = the `workload_boundary_arn` output of `infra/`.
  Otherwise `tf-apply` is denied, which is intentional.

## Differences from this repo's workflows

Only the `env:` block differs, and [sync.py](sync.py) generates the copies:

| Setting | aws-optimization-agent | infra-live |
| --- | --- | --- |
| Stacks planned | `infra`, `sandbox` | every `stacks/*` |
| Stacks applied by CI | `sandbox` (infra/ is applied by hand) | every `stacks/*` |
| State key prefix | (none) | `infra-live/` |

After changing a workflow in `.github/workflows/`, run `python3 pipelines/sync.py`.
A test fails if the copies are stale.

## Setting up infra-live

1. Copy the files above into infra-live and push them (the first push is the
   only one allowed straight to `main`; after step 2, everything goes through PRs).
2. `scripts/github_setup.sh RonnieBoi1996/infra-live all` (from this repo). It
   sets the secrets, the `production` environment and the rulesets, as described
   in [docs/github-setup.md](../docs/github-setup.md).

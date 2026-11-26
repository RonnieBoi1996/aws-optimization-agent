# The sandbox stack: test fixtures for the optimizer agents. M4 eval fixtures
# (known problems to find) are in fixtures.tf and network.tf. Keep them tiny.

# A resource group listing everything tagged environment = sandbox, so you can
# see (and later destroy) the whole sandbox in one place. Free.
resource "aws_resourcegroups_group" "sandbox" {
  name = "optimizer-sandbox"
  # Resource Groups only allow letters, digits, spaces and _ . - in descriptions.
  description = "All resources tagged environment sandbox. Optimizer test fixtures."

  resource_query {
    query = jsonencode({
      ResourceTypeFilters = ["AWS::AllSupported"]
      TagFilters = [{
        Key    = "environment"
        Values = ["sandbox"]
      }]
    })
  }
}

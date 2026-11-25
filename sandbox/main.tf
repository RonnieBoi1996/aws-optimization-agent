# The sandbox is where M4 seeds known problems (idle EC2, unattached EBS, ...)
# for the agents to find. Keep every resource tiny and cheap.

# A resource group listing everything tagged environment = sandbox, so you can
# see (and later destroy) the whole sandbox in one place. Free.
resource "aws_resourcegroups_group" "sandbox" {
  name        = "optimizer-sandbox"
  description = "All resources tagged environment=sandbox (optimizer test fixtures)."

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

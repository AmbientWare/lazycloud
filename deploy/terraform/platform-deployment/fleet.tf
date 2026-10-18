# The US regions the fleet buys in, the deployment's own first. Each gets a
# copy of the layer bucket (storage.tf), and the scheduler makes or adopts
# the fleet network there in every account (compute.EnsureNetworks). The
# node images LAZYCLOUD_FLEET_IMAGES names per region come from the Node
# images workflow.
locals {
  fleet_regions = toset([var.region, "us-east-2", "us-west-1", "us-west-2"])
}

# The scheduler owns the fleet networks this deployment once made and
# adopts them by their lazycloud:fleet tag; Terraform keeps none of them.
removed {
  from = module.fleet
  lifecycle {
    destroy = false
  }
}

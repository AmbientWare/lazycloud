# The US regions the fleet buys in, the deployment's own first. Each gets a
# fleet network and a copy of the layer bucket (storage.tf); both reach the
# server's settings from here. The node images LAZYCLOUD_FLEET_IMAGES names
# per region come from the Node images workflow.
locals {
  fleet_regions  = toset([var.region, "us-east-2", "us-west-1", "us-west-2"])
  fleet_tags     = { "lazycloud:fleet" = local.fleet_name }
  fleet_networks = { for region, network in module.fleet : region => network.network }
}

module "fleet" {
  for_each = local.fleet_regions
  source   = "./fleet-network"
  region   = each.key
  name     = "${var.deployment}-fleet"
  cidr     = var.fleet_cidr
  tags     = local.fleet_tags
}

moved {
  from = module.fleet_us_east_1
  to   = module.fleet["us-east-1"]
}

moved {
  from = module.fleet_us_east_2
  to   = module.fleet["us-east-2"]
}

moved {
  from = module.fleet_us_west_1
  to   = module.fleet["us-west-1"]
}

moved {
  from = module.fleet_us_west_2
  to   = module.fleet["us-west-2"]
}

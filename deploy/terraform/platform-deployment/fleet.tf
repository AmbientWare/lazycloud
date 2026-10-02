# The fleet's networks, one per US region the compute owner buys in. A new
# region is a provider alias in versions.tf, a module call here and an entry
# in fleet_networks.
locals {
  fleet_tags = { "lazycloud:fleet" = local.fleet_name }
  fleet_networks = {
    (var.region) = module.fleet_us_east_1.network
    "us-east-2"  = module.fleet_us_east_2.network
    "us-west-1"  = module.fleet_us_west_1.network
    "us-west-2"  = module.fleet_us_west_2.network
  }
}

module "fleet_us_east_1" {
  source = "./fleet-network"
  name   = "${var.deployment}-fleet"
  cidr   = var.fleet_cidr
  tags   = local.fleet_tags
}

module "fleet_us_east_2" {
  source    = "./fleet-network"
  providers = { aws = aws.us_east_2 }
  name      = "${var.deployment}-fleet"
  cidr      = var.fleet_cidr
  tags      = local.fleet_tags
}

module "fleet_us_west_1" {
  source    = "./fleet-network"
  providers = { aws = aws.us_west_1 }
  name      = "${var.deployment}-fleet"
  cidr      = var.fleet_cidr
  tags      = local.fleet_tags
}

module "fleet_us_west_2" {
  source    = "./fleet-network"
  providers = { aws = aws.us_west_2 }
  name      = "${var.deployment}-fleet"
  cidr      = var.fleet_cidr
  tags      = local.fleet_tags
}

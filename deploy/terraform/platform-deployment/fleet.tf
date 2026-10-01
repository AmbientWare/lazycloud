# The fleet's regional networks in this account. The scheduler launches
# platform hosts into them (LAZYCLOUD_FLEET_NETWORKS); the node image bake
# finds its subnet by their tags. A new region is a provider alias in
# versions.tf, a module call here and an entry in fleet_networks.
locals {
  fleet_launch_tag = { "cloud-pool:managed-by" = "control-plane" }

  fleet_networks = {
    (var.region) = module.fleet
    "us-east-2"  = module.fleet_ohio
    "us-west-1"  = module.fleet_california
    "us-west-2"  = module.fleet_west
  }
}

module "fleet" {
  source = "./fleet-network"

  deployment = var.deployment
  name       = "${var.deployment}-fleet"
  cidr       = var.fleet_cidr
  launch_tag = local.fleet_launch_tag
}

module "fleet_west" {
  source    = "./fleet-network"
  providers = { aws = aws.west }

  deployment = var.deployment
  name       = "${var.deployment}-fleet-west"
  cidr       = var.fleet_cidr
  launch_tag = local.fleet_launch_tag
}

module "fleet_ohio" {
  source    = "./fleet-network"
  providers = { aws = aws.ohio }

  deployment = var.deployment
  name       = "${var.deployment}-fleet-ohio"
  cidr       = var.fleet_cidr
  launch_tag = local.fleet_launch_tag
}

module "fleet_california" {
  source    = "./fleet-network"
  providers = { aws = aws.california }

  deployment = var.deployment
  name       = "${var.deployment}-fleet-california"
  cidr       = var.fleet_cidr
  launch_tag = local.fleet_launch_tag
}

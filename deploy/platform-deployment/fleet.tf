# The shared fleet: capacity in our own account, declared here rather than
# created by an authorization stack.
#
# A customer's account is one we hold no credentials for, so we hand them a
# CloudFormation template and read its outputs. Ours is a deployment we own, so
# it is declared, repeatable, and overwritten on apply. The control plane reaches
# it through existing-role mode, giving it this network by configuration.
#
# The connection role's policy is not written here. `connection-role-policy.json`
# is rendered from `provider_aws.connection_policy`, the same owner the customer
# template renders, and CI fails when the committed file stops matching it.

# The tag the connection policy launches by.
#
# `ec2:RunInstances` is granted on a subnet and a security group only where they
# carry it, so a network without it is one no pool can launch into: Auto Scaling
# checks the caller's authority over the launch template's resources and reports
# the refusal against the template, naming nothing about a subnet.
#
# A customer's network gets this from the authorization stack, which creates the
# VPC, subnets and the security group and tags each. Ours is built here
# instead, so it is tagged here, and the two networks are the same shape to the
# policy that reads them.
locals {
  fleet_launch_tag = { "cloud-pool:managed-by" = "control-plane" }

  # Published in the descriptor as `fleet.networks`. A new region is a provider
  # alias in versions.tf, a module call below, and an entry here, and must also
  # be allowed in `compute.aws_configuration`, which owns the product's region
  # list for our fleet and customer connections alike.
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

moved {
  from = aws_vpc.fleet
  to   = module.fleet.aws_vpc.this
}

moved {
  from = aws_internet_gateway.fleet
  to   = module.fleet.aws_internet_gateway.this
}

moved {
  from = aws_subnet.fleet
  to   = module.fleet.aws_subnet.this
}

moved {
  from = aws_route_table.fleet
  to   = module.fleet.aws_route_table.this
}

moved {
  from = aws_route_table_association.fleet
  to   = module.fleet.aws_route_table_association.this
}

moved {
  from = aws_security_group.fleet_node
  to   = module.fleet.aws_security_group.node
}

moved {
  from = aws_vpc_security_group_egress_rule.fleet_node
  to   = module.fleet.aws_vpc_security_group_egress_rule.node
}

moved {
  from = aws_vpc_endpoint.fleet_storage
  to   = module.fleet.aws_vpc_endpoint.storage
}

moved {
  from = aws_vpc.fleet_west
  to   = module.fleet_west.aws_vpc.this
}

moved {
  from = aws_internet_gateway.fleet_west
  to   = module.fleet_west.aws_internet_gateway.this
}

moved {
  from = aws_subnet.fleet_west
  to   = module.fleet_west.aws_subnet.this
}

moved {
  from = aws_route_table.fleet_west
  to   = module.fleet_west.aws_route_table.this
}

moved {
  from = aws_route_table_association.fleet_west
  to   = module.fleet_west.aws_route_table_association.this
}

moved {
  from = aws_security_group.fleet_west_node
  to   = module.fleet_west.aws_security_group.node
}

moved {
  from = aws_vpc_security_group_egress_rule.fleet_west_node
  to   = module.fleet_west.aws_vpc_security_group_egress_rule.node
}

moved {
  from = aws_vpc_endpoint.fleet_west_storage
  to   = module.fleet_west.aws_vpc_endpoint.storage
}

# The role the control plane assumes to manage this account's capacity. Same
# shape a customer's connection role has, because it is the same role: an
# external ID it must enforce, and the platform principal as the only trustee.
resource "aws_iam_role" "fleet_connection" {
  name        = "${var.deployment}-compute-connection"
  description = "Capacity management in the platform's own account."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.control_principal.arn }
        Action    = "sts:AssumeRole"
        # Enforced rather than decorative: connection validation proves the far
        # side rejects an assume-role without it, and a role that does not
        # enforce it fails validation with ExternalIdNotEnforced.
        Condition = {
          StringEquals = { "sts:ExternalId" = random_password.fleet_external_id.result }
        }
      },
      {
        # Its own statement, and deliberately unconditioned. The session that
        # reaches here carries tags, and propagating them is a second action AWS
        # authorizes separately. `sts:ExternalId` is a parameter of AssumeRole
        # and is absent from the request context of TagSession, so a
        # `StringEquals` on it can never match and refuses the whole call.
        #
        # Granting it alone confers nothing: tags may only be attached to an
        # assume-role the statement above has already allowed on its own terms,
        # external ID included.
        Effect    = "Allow"
        Principal = { AWS = aws_iam_role.control_principal.arn }
        Action    = "sts:TagSession"
      },
    ]
  })
}

# The one value this module generates rather than declaring a container for, and
# the exception to the rule in AGENTS.md. Both sides have to agree on it: the
# role's trust condition and the connection record the control plane holds, and
# nothing else can create both consistently.
#
# It is a confused-deputy guard rather than an authenticator. Presenting it grants
# nothing without also being the platform principal the role trusts, which is the
# separately verified identity that makes storing it here acceptable.
resource "random_password" "fleet_external_id" {
  length  = 48
  special = false
}

resource "aws_iam_role_policy" "fleet_connection" {
  name = "managed-compute-control"
  role = aws_iam_role.fleet_connection.name

  # Generated, never hand-written. See connection-role-policy.json.
  policy = file("${path.module}/connection-role-policy.json")
}

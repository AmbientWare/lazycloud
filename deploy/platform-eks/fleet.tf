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

resource "aws_vpc" "fleet" {
  cidr_block           = var.fleet_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.deployment}-fleet" }
}

resource "aws_internet_gateway" "fleet" {
  vpc_id = aws_vpc.fleet.id

  tags = { Name = "${var.deployment}-fleet" }
}

# Exactly two, in two zones. `AwsAccountNetwork` accepts no other count, and an
# Auto Scaling group spanning one zone cannot replace a node when that zone is
# what failed.
resource "aws_subnet" "fleet" {
  count = 2

  vpc_id                  = aws_vpc.fleet.id
  cidr_block              = cidrsubnet(var.fleet_cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.deployment}-fleet-${data.aws_availability_zones.available.names[count.index]}" }
}

resource "aws_route_table" "fleet" {
  vpc_id = aws_vpc.fleet.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.fleet.id
  }

  tags = { Name = "${var.deployment}-fleet" }
}

resource "aws_route_table_association" "fleet" {
  count = 2

  subnet_id      = aws_subnet.fleet[count.index].id
  route_table_id = aws_route_table.fleet.id
}

# Public subnets with public addressing rather than private ones behind NAT. The
# nodes need egress to pull images and reach the tailnet, they accept nothing
# inbound, and a NAT gateway per zone would cost more than the instances it
# serves at this size.
resource "aws_security_group" "fleet_node" {
  name        = "${var.deployment}-fleet-node"
  description = "Shared fleet nodes: egress only."
  vpc_id      = aws_vpc.fleet.id

  tags = { Name = "${var.deployment}-fleet-node" }
}

# No inbound rule, and none should be added. A worker is reached over the
# tailnet, which is a session the node itself establishes outbound.
resource "aws_vpc_security_group_egress_rule" "fleet_node" {
  security_group_id = aws_security_group.fleet_node.id
  description       = "Image pulls, the tailnet, and the control plane."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
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

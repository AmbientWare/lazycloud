data "aws_availability_zones" "available" {
  state = "available"
}

# Only the control plane's network is declared here.
#
# The shared fleet's VPC, subnets, security group and node identity are created
# by the account connection stack the control plane generates, and its outputs
# are what the pool launches into. Reproducing that network in Terraform would
# mean reproducing the connection role's policy too, which is generated in
# `provider_aws/account_connection.py` against CloudFormation refs and would
# drift from its owner the first time it changed.
#
# The platform account therefore connects to itself through the same managed
# flow a customer uses. That is the route that gets debugged, and the control
# stack already anticipates it: "a customer account can be this account".

resource "aws_vpc" "control_plane" {
  cidr_block           = var.control_plane_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.deployment}-control-plane" }
}

resource "aws_internet_gateway" "control_plane" {
  vpc_id = aws_vpc.control_plane.id

  tags = { Name = "${var.deployment}-control-plane" }
}

resource "aws_subnet" "control_plane" {
  vpc_id                  = aws_vpc.control_plane.id
  cidr_block              = cidrsubnet(var.control_plane_cidr, 8, 1)
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = { Name = "${var.deployment}-control-plane" }
}

resource "aws_route_table" "control_plane" {
  vpc_id = aws_vpc.control_plane.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.control_plane.id
  }

  tags = { Name = "${var.deployment}-control-plane" }
}

resource "aws_route_table_association" "control_plane" {
  subnet_id      = aws_subnet.control_plane.id
  route_table_id = aws_route_table.control_plane.id
}

resource "aws_security_group" "control_plane" {
  name        = "${var.deployment}-control-plane"
  description = "Control plane host: egress, plus whatever operator_ingress_cidrs opens."
  vpc_id      = aws_vpc.control_plane.id

  tags = { Name = "${var.deployment}-control-plane" }
}

# Public traffic arrives through the Cloudflare tunnel and workers arrive over the
# tailnet, both of which the host dials outbound. Operators use SSM Session
# Manager, which is also outbound. Nothing here needs an inbound rule.
resource "aws_vpc_security_group_egress_rule" "control_plane" {
  security_group_id = aws_security_group.control_plane.id
  description       = "Tunnel, tailnet, database, image pulls."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "control_plane_operator" {
  for_each = toset(var.operator_ingress_cidrs)

  security_group_id = aws_security_group.control_plane.id
  description       = "Operator access opened deliberately."
  cidr_ipv4         = each.value
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

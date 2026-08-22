data "aws_availability_zones" "available" {
  state = "available"
}

# Only the cluster's network is declared here.
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

resource "aws_vpc" "cluster" {
  cidr_block = var.control_plane_cidr
  # Both required by EKS: nodes resolve the cluster endpoint by name, and the
  # in-cluster DNS answers for services out of the VPC resolver.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.deployment}-cluster" }
}

resource "aws_internet_gateway" "cluster" {
  vpc_id = aws_vpc.cluster.id

  tags = { Name = "${var.deployment}-cluster" }
}

# Public subnets, and no NAT gateway.
#
# Everything these nodes reach is outbound and public: ECR, Secrets Manager,
# PlanetScale, Cloudflare, the tailnet coordination server. A NAT gateway would
# add an hourly charge and a per-gigabyte one to reach the same endpoints a
# public subnet reaches for free, and buy privacy for nodes whose inbound is
# already closed by their security group.
#
# One per zone because EKS requires at least two, and because a control plane
# that cannot be rescheduled into another zone is one zone's outage away from
# being down.
resource "aws_subnet" "cluster" {
  count = var.cluster_subnet_count

  vpc_id                  = aws_vpc.cluster.id
  cidr_block              = cidrsubnet(var.control_plane_cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.deployment}-cluster-${count.index}"
    # Read by the AWS load balancer controller when it places a public load
    # balancer. Absent, it finds no subnet and reports no eligible subnets rather
    # than naming the tag it wanted.
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_route_table" "cluster" {
  vpc_id = aws_vpc.cluster.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.cluster.id
  }

  tags = { Name = "${var.deployment}-cluster" }
}

resource "aws_route_table_association" "cluster" {
  count = var.cluster_subnet_count

  subnet_id      = aws_subnet.cluster[count.index].id
  route_table_id = aws_route_table.cluster.id
}

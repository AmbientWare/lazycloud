data "aws_availability_zones" "west" {
  provider = aws.west
  state    = "available"

  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

resource "aws_vpc" "fleet_west" {
  provider = aws.west
  # Regional VPCs do not peer; workers connect through their outbound overlay.
  cidr_block           = var.fleet_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(local.fleet_launch_tag, { Name = "${var.deployment}-fleet-west" })
}

resource "aws_internet_gateway" "fleet_west" {
  provider = aws.west
  vpc_id   = aws_vpc.fleet_west.id
  tags     = { Name = "${var.deployment}-fleet-west" }
}

resource "aws_subnet" "fleet_west" {
  provider                = aws.west
  count                   = length(data.aws_availability_zones.west.names)
  vpc_id                  = aws_vpc.fleet_west.id
  cidr_block              = cidrsubnet(var.fleet_cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.west.names[count.index]
  map_public_ip_on_launch = true
  tags = merge(local.fleet_launch_tag, {
    Name = "${var.deployment}-fleet-${data.aws_availability_zones.west.names[count.index]}"
  })
}

resource "aws_route_table" "fleet_west" {
  provider = aws.west
  vpc_id   = aws_vpc.fleet_west.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.fleet_west.id
  }

  tags = { Name = "${var.deployment}-fleet-west" }
}

resource "aws_route_table_association" "fleet_west" {
  provider       = aws.west
  count          = length(aws_subnet.fleet_west)
  subnet_id      = aws_subnet.fleet_west[count.index].id
  route_table_id = aws_route_table.fleet_west.id
}

resource "aws_security_group" "fleet_west_node" {
  provider    = aws.west
  name        = "${var.deployment}-fleet-west-node"
  description = "Shared fleet nodes: egress only."
  vpc_id      = aws_vpc.fleet_west.id
  tags        = merge(local.fleet_launch_tag, { Name = "${var.deployment}-fleet-west-node" })
}

resource "aws_vpc_security_group_egress_rule" "fleet_west_node" {
  provider          = aws.west
  security_group_id = aws_security_group.fleet_west_node.id
  description       = "Image pulls, outbound agent TLS, and the control plane."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_endpoint" "fleet_west_storage" {
  provider          = aws.west
  vpc_id            = aws_vpc.fleet_west.id
  service_name      = "com.amazonaws.us-west-2.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.fleet_west.id]
  tags              = { Name = "${var.deployment}-fleet-west-storage" }
}

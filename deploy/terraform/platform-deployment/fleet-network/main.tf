# One regional fleet network: a VPC with a public subnet per zone, an
# egress-only security group and an S3 gateway endpoint. Hosts accept nothing
# inbound and reach out for images, object storage and the server, so a NAT
# gateway per zone would cost more than the hosts it serves.

terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "name" {
  type = string
}

variable "cidr" {
  type = string
}

# lazycloud:fleet=<fleet>, which the node image bake finds its subnet by.
variable "tags" {
  type = map(string)
}

data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = merge(var.tags, { Name = var.name })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "this" {
  count                   = length(data.aws_availability_zones.available.names)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = merge(var.tags, { Name = "${var.name}-${data.aws_availability_zones.available.names[count.index]}" })
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = var.name }
}

resource "aws_route_table_association" "this" {
  count          = length(aws_subnet.this)
  subnet_id      = aws_subnet.this[count.index].id
  route_table_id = aws_route_table.this.id
}

resource "aws_security_group" "node" {
  name        = "${var.name}-node"
  description = "Fleet hosts: egress only."
  vpc_id      = aws_vpc.this.id
  tags        = merge(var.tags, { Name = "${var.name}-node" })
}

resource "aws_vpc_security_group_egress_rule" "node" {
  security_group_id = aws_security_group.node.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.this.id]
  tags              = { Name = "${var.name}-s3" }
}

# This region's entry of LAZYCLOUD_FLEET_NETWORKS (compute.Network).
output "network" {
  value = {
    vpc_id            = aws_vpc.this.id
    security_group_id = aws_security_group.node.id
    subnets = [for index, subnet in aws_subnet.this : {
      id      = subnet.id
      zone    = subnet.availability_zone
      zone_id = data.aws_availability_zones.available.zone_ids[index]
    }]
  }
}

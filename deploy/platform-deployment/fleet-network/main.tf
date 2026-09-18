# One regional fleet network. The root module calls this once per region with
# that region's provider; Terraform cannot create providers from a list, so the
# alias is the one thing a new region still declares by hand.
#
# Public subnets with public addressing rather than private ones behind NAT. The
# nodes need egress to pull images and reach the control plane, they accept
# nothing inbound, and a NAT gateway per zone would cost more than the instances
# it serves at this size. Regional VPCs do not peer; workers connect through
# their outbound overlay.

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
    }
  }
}

variable "deployment" {
  type = string
}

# The `Name` prefix of everything here. Security group names are immutable, so
# it stays what each region was first created with.
variable "name" {
  type = string
}

variable "cidr" {
  type = string
}

# The tag the connection policy launches by. `ec2:RunInstances` is granted on a
# subnet and a security group only where they carry it.
variable "launch_tag" {
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

  tags = merge(var.launch_tag, { Name = var.name })
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = var.name }
}

resource "aws_subnet" "this" {
  count = length(data.aws_availability_zones.available.names)

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = merge(
    var.launch_tag,
    { Name = "${var.deployment}-fleet-${data.aws_availability_zones.available.names[count.index]}" },
  )
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
  count = length(aws_subnet.this)

  subnet_id      = aws_subnet.this[count.index].id
  route_table_id = aws_route_table.this.id
}

resource "aws_security_group" "node" {
  name        = "${var.name}-node"
  description = "Shared fleet nodes: egress only."
  vpc_id      = aws_vpc.this.id

  tags = merge(var.launch_tag, { Name = "${var.name}-node" })
}

# No inbound rule, and none should be added. A worker is reached over the
# agent tunnel, which the node establishes outbound over TLS.
resource "aws_vpc_security_group_egress_rule" "node" {
  security_group_id = aws_security_group.node.id
  description       = "Image pulls, outbound agent TLS, and the control plane."
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_endpoint" "storage" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.this.id]
  tags              = { Name = "${var.name}-storage" }
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "subnet_ids" {
  value = aws_subnet.this[*].id
}

output "security_group_id" {
  value = aws_security_group.node.id
}

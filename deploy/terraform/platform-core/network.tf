data "aws_availability_zones" "available" {
  state = "available"
}

# Public subnets without NAT: nodes only reach out (ECR, Secrets Manager,
# PlanetScale, Cloudflare, AWS APIs) and accept nothing their security group
# does not admit, so a NAT gateway would cost hourly and per gigabyte for no
# privacy gained. The fleet's networks are each deployment's.
resource "aws_vpc" "cluster" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.name}-cluster" }
}

resource "aws_internet_gateway" "cluster" {
  vpc_id = aws_vpc.cluster.id
  tags   = { Name = "${var.name}-cluster" }
}

resource "aws_subnet" "cluster" {
  count                   = var.zones
  vpc_id                  = aws_vpc.cluster.id
  cidr_block              = cidrsubnet(var.cidr, 8, count.index + 1)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  # Where the load balancer controller places internet-facing NLBs.
  tags = { Name = "${var.name}-cluster-${count.index}", "kubernetes.io/role/elb" = "1" }
}

resource "aws_route_table" "cluster" {
  vpc_id = aws_vpc.cluster.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.cluster.id
  }

  tags = { Name = "${var.name}-cluster" }
}

resource "aws_route_table_association" "cluster" {
  count          = var.zones
  subnet_id      = aws_subnet.cluster[count.index].id
  route_table_id = aws_route_table.cluster.id
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.cluster.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.cluster.id]
  tags              = { Name = "${var.name}-s3" }
}

resource "random_uuid" "fleet_provider" {}

variable "fleet_node_identity_name" {
  description = "Durable IAM name when adopting an existing platform node identity."
  type        = string
  default     = null
}

locals {
  fleet_node_identity_name = coalesce(var.fleet_node_identity_name, "compute-node-${var.deployment}")
}

resource "aws_iam_role" "fleet_node" {
  name = local.fleet_node_identity_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { "cloud-pool:managed-by" = "control-plane" }
}

resource "aws_iam_instance_profile" "fleet_node" {
  name = local.fleet_node_identity_name
  role = aws_iam_role.fleet_node.name
  tags = { "cloud-pool:managed-by" = "control-plane" }
}

resource "aws_iam_role_policy" "fleet_node_diagnostics" {
  name   = "managed-node-diagnostics"
  role   = aws_iam_role.fleet_node.name
  policy = file("${path.module}/node-diagnostics-policy.json")
}

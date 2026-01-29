# -----------------------------------------------------------------------------
# Firewall
# -----------------------------------------------------------------------------

data "http" "my_ip" {
  url = "https://ipv4.icanhazip.com"
  retry {
    attempts     = 3
    min_delay_ms = 1000
    max_delay_ms = 2000
  }
}

locals {
  # Use configured CIDRs if provided, otherwise fall back to auto-detected IP
  management_cidrs = length(var.management_cidrs) > 0 ? var.management_cidrs : ["${chomp(data.http.my_ip.response_body)}/32"]

  # All node IPs for intra-cluster communication
  all_node_ips = concat(
    [for ip in local.cp_public_ipv4 : "${ip}/32"],
    [for ip in local.worker_public_ipv4 : "${ip}/32"],
  )

  # Worker IPs for ingress rules
  worker_ips = [for ip in local.worker_public_ipv4 : "${ip}/32"]
}

resource "hcloud_firewall" "this" {
  name = var.cluster_name

  # External access rules (from management IPs)
  rule {
    description = "Allow Kube API from management IPs"
    direction   = "in"
    protocol    = "tcp"
    port        = "6443"
    source_ips  = local.management_cidrs
  }

  rule {
    description = "Allow Talos API from management IPs"
    direction   = "in"
    protocol    = "tcp"
    port        = "50000"
    source_ips  = local.management_cidrs
  }

  # Intra-cluster communication rules
  rule {
    description = "Allow Kube API from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "6443"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow etcd from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "2379-2380"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow kubelet from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "10250"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow kube-scheduler from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "10259"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow kube-controller-manager from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "10257"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow Cilium VXLAN overlay from cluster nodes"
    direction   = "in"
    protocol    = "udp"
    port        = "8472"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow Cilium health checks from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "4240"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow Talos API from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "50000"
    source_ips  = local.all_node_ips
  }

  rule {
    description = "Allow Talos trustd from cluster nodes"
    direction   = "in"
    protocol    = "tcp"
    port        = "50001"
    source_ips  = local.all_node_ips
  }

  # Public ingress rules (for NGINX ingress controller)
  dynamic "rule" {
    for_each = var.enable_public_ingress ? [1] : []
    content {
      description = "Allow HTTP from anywhere"
      direction   = "in"
      protocol    = "tcp"
      port        = "80"
      source_ips  = ["0.0.0.0/0", "::/0"]
    }
  }

  dynamic "rule" {
    for_each = var.enable_public_ingress ? [1] : []
    content {
      description = "Allow HTTPS from anywhere"
      direction   = "in"
      protocol    = "tcp"
      port        = "443"
      source_ips  = ["0.0.0.0/0", "::/0"]
    }
  }

  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
  }
}

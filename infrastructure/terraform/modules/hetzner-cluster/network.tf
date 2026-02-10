# -----------------------------------------------------------------------------
# Network
# -----------------------------------------------------------------------------

data "hcloud_location" "this" {
  name = var.location
}

resource "hcloud_network" "this" {
  name     = var.cluster_name
  ip_range = var.network_ipv4_cidr
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
  }
}

resource "hcloud_network_subnet" "nodes" {
  network_id   = hcloud_network.this.id
  type         = "cloud"
  network_zone = data.hcloud_location.this.network_zone
  ip_range     = var.node_ipv4_cidr
}

# -----------------------------------------------------------------------------
# Primary IPs (stable across server rebuilds)
# -----------------------------------------------------------------------------

resource "hcloud_primary_ip" "control_plane" {
  count         = var.control_plane_count
  name          = "${var.cluster_name}-control-plane-${count.index + 1}-ipv4"
  type          = "ipv4"
  assignee_type = "server"
  auto_delete   = false
  location      = var.location
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
    role       = "control-plane"
  }
}

resource "hcloud_primary_ip" "platform" {
  count         = var.platform_count
  name          = "${var.cluster_name}-platform-${count.index + 1}-ipv4"
  type          = "ipv4"
  assignee_type = "server"
  auto_delete   = false
  location      = var.location
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
    role       = "platform"
  }
}

# -----------------------------------------------------------------------------
# Floating IP (stable VIP for control plane, survives node failover)
# -----------------------------------------------------------------------------

resource "hcloud_floating_ip" "control_plane_vip" {
  name          = "${var.cluster_name}-control-plane-vip"
  type          = "ipv4"
  home_location = var.location
  description   = "Control plane VIP for kube API"
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
    role       = "control-plane"
  }
}

resource "hcloud_floating_ip_assignment" "control_plane_vip" {
  floating_ip_id = hcloud_floating_ip.control_plane_vip.id
  server_id      = hcloud_server.control_plane[0].id
}

# -----------------------------------------------------------------------------
# Computed IP lists
# -----------------------------------------------------------------------------

locals {
  cp_vip_ipv4     = hcloud_floating_ip.control_plane_vip.ip_address
  cp_public_ipv4  = [for ip in hcloud_primary_ip.control_plane : ip.ip_address]
  cp_private_ipv4 = [for i in range(var.control_plane_count) : cidrhost(var.node_ipv4_cidr, i + 101)]

  platform_public_ipv4  = [for ip in hcloud_primary_ip.platform : ip.ip_address]
  platform_private_ipv4 = [for i in range(var.platform_count) : cidrhost(var.node_ipv4_cidr, i + 151)]
}

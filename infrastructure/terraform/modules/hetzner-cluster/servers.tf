# -----------------------------------------------------------------------------
# Talos OS image snapshot (built via Packer, looked up here)
# -----------------------------------------------------------------------------

data "hcloud_image" "talos" {
  with_selector     = "os=talos"
  with_architecture = "x86"
  most_recent       = true
}

# -----------------------------------------------------------------------------
# Placement groups (spread servers across hosts)
# -----------------------------------------------------------------------------

resource "hcloud_placement_group" "control_plane" {
  name = "${var.cluster_name}-control-plane"
  type = "spread"
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
  }
}

resource "hcloud_placement_group" "platform" {
  name = "${var.cluster_name}-platform"
  type = "spread"
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
  }
}


# -----------------------------------------------------------------------------
# SSH key (required by Hetzner even though Talos doesn't use it)
# -----------------------------------------------------------------------------

resource "tls_private_key" "ssh" {
  algorithm = "ED25519"
}

resource "hcloud_ssh_key" "this" {
  name       = "${var.cluster_name}-default"
  public_key = tls_private_key.ssh.public_key_openssh
  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
  }
}

# -----------------------------------------------------------------------------
# Control plane servers
# -----------------------------------------------------------------------------

resource "hcloud_server" "control_plane" {
  count              = var.control_plane_count
  name               = "${var.cluster_name}-control-plane-${count.index + 1}"
  location           = var.location
  image              = data.hcloud_image.talos.id
  server_type        = var.control_plane_type
  user_data          = data.talos_machine_configuration.control_plane[count.index].machine_configuration
  ssh_keys           = [hcloud_ssh_key.this.id]
  placement_group_id = hcloud_placement_group.control_plane.id
  firewall_ids       = [hcloud_firewall.this.id]

  labels = {
    cluster    = var.cluster_name
    cluster_id = var.cluster_id
    role       = "control-plane"
  }

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.control_plane[count.index].id
    ipv6_enabled = false
  }

  network {
    network_id = hcloud_network.this.id
    ip         = local.cp_private_ipv4[count.index]
    alias_ips  = []
  }

  depends_on = [hcloud_network_subnet.nodes]

  lifecycle {
    ignore_changes = [user_data, image]
  }
}

# -----------------------------------------------------------------------------
# Platform servers (fixed pool — runs LazyCloud infrastructure)
# -----------------------------------------------------------------------------

resource "hcloud_server" "platform" {
  count              = var.platform_count
  name               = "${var.cluster_name}-platform-${count.index + 1}"
  location           = var.location
  image              = data.hcloud_image.talos.id
  server_type        = var.platform_type
  user_data          = data.talos_machine_configuration.platform[count.index].machine_configuration
  ssh_keys           = [hcloud_ssh_key.this.id]
  placement_group_id = hcloud_placement_group.platform.id
  firewall_ids       = [hcloud_firewall.this.id]

  labels = {
    cluster     = var.cluster_name
    cluster_id  = var.cluster_id
    role        = "platform"
    server_type = var.platform_type
  }

  public_net {
    ipv4_enabled = true
    ipv4         = hcloud_primary_ip.platform[count.index].id
    ipv6_enabled = false
  }

  network {
    network_id = hcloud_network.this.id
    ip         = local.platform_private_ipv4[count.index]
    alias_ips  = []
  }

  depends_on = [hcloud_network_subnet.nodes]

  lifecycle {
    ignore_changes = [user_data, image]
  }
}


packer {
  required_plugins {
    hcloud = {
      source  = "github.com/hetznercloud/hcloud"
      version = ">= 1.6.0"
    }
  }
}

variable "talos_version" {
  type    = string
  default = "v1.12.2"
}

variable "server_location" {
  type    = string
  default = "ash"
}

locals {
  # Schematic includes: siderolabs/gvisor only
  # (iscsi-tools removed — JuiceFS CSI runs as pods, no host extension needed)
  #
  # To regenerate schematic ID after changing extensions:
  #   curl -sX POST https://factory.talos.dev/schematics \
  #     -H 'Content-Type: application/yaml' \
  #     -d 'customization:
  #           systemExtensions:
  #             officialExtensions:
  #               - siderolabs/gvisor' | jq -r .id
  image_url = "https://factory.talos.dev/image/${var.talos_schematic_id}/${var.talos_version}/hcloud-amd64.raw.xz"
}

variable "talos_schematic_id" {
  type        = string
  description = "Talos factory schematic ID (gVisor only). Regenerate if changing extensions."
  default     = "d9ff89777e246792e7642abd3220a616afb4e49822382e4213a2e528ab826fe5"
}

source "hcloud" "talos" {
  rescue       = "linux64"
  image        = "debian-12"
  location     = var.server_location
  server_type  = "cpx11"
  ssh_username = "root"

  snapshot_name   = "talos-${var.talos_version}"
  snapshot_labels = {
    os = "talos"
  }
}

build {
  sources = ["source.hcloud.talos"]

  provisioner "shell" {
    inline = [
      "set -ex",
      "apt-get install -y wget xz-utils",
      "wget -q -O /tmp/talos.raw.xz '${local.image_url}'",
      "xz -d /tmp/talos.raw.xz",
      "dd if=/tmp/talos.raw of=/dev/sda bs=4M status=progress",
      "sync",
    ]
  }
}

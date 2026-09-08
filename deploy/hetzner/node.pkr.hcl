packer {
  required_plugins {
    hcloud = {
      version = "= 1.8.0"
      source  = "github.com/hetznercloud/hcloud"
    }
  }
}

variable "base_image_id" {
  type = string
  validation {
    condition     = can(regex("^[1-9][0-9]*$", var.base_image_id))
    error_message = "Use the immutable ID of an Ubuntu 24.04 x86 image."
  }
}

variable "location" {
  type = string
}

variable "bake_id" {
  type = string
}

variable "recipe_sha256" {
  type = string
}

variable "runtime_installer" {
  type = string
}

variable "manifest_path" {
  type = string
}

source "hcloud" "cpu" {
  image                   = var.base_image_id
  location                = var.location
  server_type             = "ccx13"
  server_name             = "lc-bake-${var.bake_id}"
  snapshot_name           = "lc-cpu-${substr(var.recipe_sha256, 0, 16)}-${var.bake_id}"
  ssh_username            = "root"
  temporary_key_pair_type = "ed25519"
  public_ipv6_disabled    = true
  server_labels = {
    lazycloud-bake = var.bake_id
  }
  ssh_keys_labels = {
    lazycloud-bake = var.bake_id
  }
  snapshot_labels = {
    lazycloud-bake    = var.bake_id
    lazycloud-release = substr(var.recipe_sha256, 0, 63)
  }
}

build {
  sources = ["source.hcloud.cpu"]

  provisioner "shell" {
    script          = var.runtime_installer
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} {{ .Path }} --runtime-only"
  }

  provisioner "shell" {
    script = "${path.root}/../node-images/prepare-ubuntu-host.sh"
  }

  post-processor "manifest" {
    output     = var.manifest_path
    strip_path = true
    custom_data = {
      recipe_sha256 = var.recipe_sha256
      base_image_id = var.base_image_id
      location      = var.location
      bake_id       = var.bake_id
    }
  }
}

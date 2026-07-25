locals {
  policy = templatefile("${path.module}/policy.json.tftpl", {
    agent_tag         = var.agent_tag
    control_plane_tag = var.control_plane_tag
    agent_proxy_port  = var.agent_proxy_port
  })
}

resource "tailscale_acl" "customer_compute" {
  acl                        = local.policy
  overwrite_existing_content = var.overwrite_existing_policy
  reset_acl_on_destroy       = false

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_tailnet
      error_message = "Refusing to manage policy until confirm_dedicated_tailnet is true."
    }

    precondition {
      condition     = var.agent_tag != var.control_plane_tag
      error_message = "agent_tag and control_plane_tag must be distinct."
    }
  }
}

resource "tailscale_oauth_client" "agent_lifecycle" {
  description = "LazyCloud ${var.environment} customer compute"
  scopes      = ["auth_keys", "devices:core"]
  tags        = [var.agent_tag]

  depends_on = [tailscale_acl.customer_compute]
}

resource "tailscale_tailnet_key" "gateway" {
  description         = "LazyCloud ${var.environment} control plane"
  reusable            = true
  ephemeral           = true
  preauthorized       = true
  expiry              = var.gateway_auth_key_expiry_seconds
  recreate_if_invalid = "always"
  tags                = [var.control_plane_tag]

  depends_on = [tailscale_acl.customer_compute]
}

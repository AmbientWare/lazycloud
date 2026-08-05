locals {
  policy = templatefile("${path.module}/policy.json.tftpl", {
    agent_tag          = var.agent_tag
    control_plane_tag  = var.control_plane_tag
    agent_proxy_port   = var.agent_proxy_port
    control_plane_port = var.control_plane_port
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
      condition     = length(distinct([var.agent_tag, var.control_plane_tag])) == 2
      error_message = "agent_tag and control_plane_tag must be distinct."
    }
  }
}

# An OAuth client may only mint auth keys for tags it owns. A node now enrols
# over the public origin and joins the tailnet with the machine key it is vended
# afterwards, so the agent tag is the only one this client ever mints for.
#
# Changing `tags` replaces the client: re-export
# LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID and _SECRET in the same change, or the
# control plane cannot mint keys at all.
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

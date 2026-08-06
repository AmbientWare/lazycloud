locals {
  policy = templatefile("${path.module}/policy.json.tftpl", {
    agent_tag             = var.agent_tag
    control_plane_tag     = var.control_plane_tag
    agent_proxy_port      = var.agent_proxy_port
    control_plane_port    = var.control_plane_port
    control_plane_service = var.control_plane_service
    tcp_ingress_port      = var.tcp_ingress_port
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

# An OAuth client may only mint auth keys for tags it owns. It mints for both:
# the agent tag for every node it enrols, and its own tag for the device the
# control plane registers when it joins the tailnet. `tailscale up` advertises no
# tag, so for the control plane this client is the only thing that can apply one.
#
# Changing `tags` replaces the client: re-export
# LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID and _SECRET in the same change, or the
# control plane cannot mint keys at all.
resource "tailscale_oauth_client" "agent_lifecycle" {
  description = "LazyCloud ${var.environment} customer compute"
  scopes      = ["auth_keys", "devices:core"]
  tags        = [var.agent_tag, var.control_plane_tag]

  depends_on = [tailscale_acl.customer_compute]
}

# Settings that live on the tailnet rather than in its policy, and that nothing
# else records. A tailnet rebuilt without these is not the same tailnet: MagicDNS
# in particular is why a service address resolves at all, and it was a console
# setting no file mentioned.
#
# The values here are the ones the tailnet already holds, so a first apply changes
# nothing. They are written down to be reproducible, not to impose a preference.
# They are tailnet-wide, which is a broader claim than the policy, so they share
# the dedicated-tailnet precondition.
resource "tailscale_dns_preferences" "customer_compute" {
  magic_dns = var.magic_dns

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_tailnet
      error_message = "Refusing to manage tailnet DNS until confirm_dedicated_tailnet is true."
    }
  }
}

resource "tailscale_dns_nameservers" "customer_compute" {
  nameservers = var.dns_nameservers

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_tailnet
      error_message = "Refusing to manage tailnet DNS until confirm_dedicated_tailnet is true."
    }
  }
}

resource "tailscale_dns_search_paths" "customer_compute" {
  search_paths = var.dns_search_paths

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_tailnet
      error_message = "Refusing to manage tailnet DNS until confirm_dedicated_tailnet is true."
    }
  }
}

resource "tailscale_tailnet_settings" "customer_compute" {
  devices_approval_on       = var.devices_approval_on
  devices_auto_updates_on   = var.devices_auto_updates_on
  devices_key_duration_days = var.devices_key_duration_days
  users_approval_on         = var.users_approval_on

  lifecycle {
    precondition {
      condition     = var.confirm_dedicated_tailnet
      error_message = "Refusing to manage tailnet settings until confirm_dedicated_tailnet is true."
    }
  }
}

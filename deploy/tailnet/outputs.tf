output "agent_oauth_client_id" {
  description = "Set as LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID in the control plane."
  value       = tailscale_oauth_client.agent_lifecycle.id
}

output "agent_oauth_client_secret" {
  description = "Store as LAZYCLOUD_TAILNET_OAUTH_CLIENT_SECRET in the deployment secret manager."
  value       = tailscale_oauth_client.agent_lifecycle.key
  sensitive   = true
}

output "gateway_auth_key" {
  description = "Store as LAZYCLOUD_TAILNET_AUTH_KEY for the gateway sidecar only."
  value       = tailscale_tailnet_key.gateway.key
  sensitive   = true
}

output "runtime_configuration" {
  description = "Non-secret Tailnet values consumed by Compose."
  value = {
    LAZYCLOUD_TAILNET_AGENT_TAG          = var.agent_tag
    LAZYCLOUD_TAILNET_CONTROL_PLANE_TAG  = var.control_plane_tag
    LAZYCLOUD_TAILNET_POOL_BOOTSTRAP_TAG = var.bootstrap_tag
  }
}

output "policy_sha256" {
  description = "Digest of the exact policy managed by this configuration."
  value       = sha256(local.policy)
}

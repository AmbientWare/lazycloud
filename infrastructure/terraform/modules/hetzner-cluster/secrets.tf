# -----------------------------------------------------------------------------
# AWS Secrets Manager — Per-Cluster Secrets
# -----------------------------------------------------------------------------
# Each cluster stores its secrets in a single JSON object containing:
#   - kubeconfig: The cluster kubeconfig for the backend to create K8s clients
#   - cloudflare_tunnel_token: The tunnel token for cloudflared pods
#
# Secret path: {prefix}/clusters/{cluster_id}
# Example: lazycloud/clusters/ash1
#
# Shared secrets (prod-secrets, shared-secrets, staging-secrets) are managed
# in the global/ terraform module, not here.
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "cluster" {
  name        = "${var.secrets_prefix}/clusters/${var.cluster_id}"
  description = "Cluster secrets for ${var.cluster_id} (kubeconfig, tunnel token)"

  # Allow immediate recreation without recovery window (for cluster rebuilds)
  recovery_window_in_days = 0

  tags = {
    cluster_id = var.cluster_id
    managed_by = "terraform"
  }
}

# Note: The secret value is set in cloudflare.tf after the tunnel token is available
# This resource just creates the secret container

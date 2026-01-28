# -----------------------------------------------------------------------------
# AWS Secrets Manager — Per-Cluster Kubeconfig
# -----------------------------------------------------------------------------
# Each cluster stores its kubeconfig in Secrets Manager for the backend to fetch.
# The backend reads all cluster kubeconfigs at startup and creates K8s clients.
#
# Shared secrets (prod-secrets, shared-secrets, staging-secrets) are managed
# in the global/ terraform module, not here.
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "kubeconfig" {
  name        = "${var.secrets_prefix}/clusters/${var.cluster_id}/kubeconfig"
  description = "Kubeconfig for cluster ${var.cluster_id}"

  tags = {
    cluster_id = var.cluster_id
    managed_by = "terraform"
  }
}

resource "aws_secretsmanager_secret_version" "kubeconfig" {
  secret_id     = aws_secretsmanager_secret.kubeconfig.id
  secret_string = talos_cluster_kubeconfig.this.kubeconfig_raw
}

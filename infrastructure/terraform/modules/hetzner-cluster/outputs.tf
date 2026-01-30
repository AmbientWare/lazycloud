# -----------------------------------------------------------------------------
# Module outputs
# -----------------------------------------------------------------------------

output "cluster_id" {
  description = "Cluster identifier"
  value       = var.cluster_id
}

output "kubeconfig" {
  description = "Kubeconfig for the cluster"
  value       = talos_cluster_kubeconfig.this.kubeconfig_raw
  sensitive   = true
}

output "talosconfig" {
  description = "Talos client configuration"
  value       = data.talos_client_configuration.this.talos_config
  sensitive   = true
}

output "control_plane_vip" {
  description = "Floating IP (VIP) for the kube API"
  value       = local.cp_vip_ipv4
}

output "control_plane_ips" {
  description = "Public IPv4 addresses of control plane nodes"
  value       = local.cp_public_ipv4
}

output "network_id" {
  description = "Hetzner network ID"
  value       = hcloud_network.this.id
}

output "kubernetes_host" {
  description = "Kubernetes API host URL"
  value       = talos_cluster_kubeconfig.this.kubernetes_client_configuration.host
}

output "kubernetes_client_certificate" {
  description = "Kubernetes client certificate (base64)"
  value       = talos_cluster_kubeconfig.this.kubernetes_client_configuration.client_certificate
  sensitive   = true
}

output "kubernetes_client_key" {
  description = "Kubernetes client key (base64)"
  value       = talos_cluster_kubeconfig.this.kubernetes_client_configuration.client_key
  sensitive   = true
}

output "kubernetes_ca_certificate" {
  description = "Kubernetes CA certificate (base64)"
  value       = talos_cluster_kubeconfig.this.kubernetes_client_configuration.ca_certificate
  sensitive   = true
}

output "argocd_admin_password" {
  description = "ArgoCD initial admin password (retrieve after apply)"
  value       = "Run: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
}

output "autoscaler_node_config_key" {
  description = "Key name used in cluster autoscaler config"
  value       = local.autoscaler_node_config_key
}

output "cluster_secret_arn" {
  description = "ARN of the cluster secrets in Secrets Manager (contains kubeconfig and tunnel token)"
  value       = aws_secretsmanager_secret.cluster.arn
}

output "cloudflare_tunnel_id" {
  description = "Cloudflare Tunnel ID for this cluster"
  value       = cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel.id
}

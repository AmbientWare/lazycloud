# -----------------------------------------------------------------------------
# Cloudflare Tunnel — Remote-Managed
# -----------------------------------------------------------------------------
# Creates a Cloudflare Tunnel for this cluster with remote management enabled.
# Tunnel routing configuration is stored in Cloudflare (not local config files).
#
# The tunnel token is stored in the per-cluster AWS secret alongside the kubeconfig.
# -----------------------------------------------------------------------------

# Provider configuration
provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

# Create the Cloudflare Tunnel (remote-managed)
resource "cloudflare_zero_trust_tunnel_cloudflared" "cluster_tunnel" {
  account_id = var.cloudflare_account_id
  name       = "lazycloud-prod-${var.cluster_id}"
  config_src = "cloudflare" # Remote-managed: config stored in Cloudflare dashboard
}

# Get the tunnel token for cloudflared to authenticate
data "cloudflare_zero_trust_tunnel_cloudflared_token" "cluster_tunnel_token" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel.id
}

# Configure tunnel ingress rules (remote-managed)
# Routes wildcard subdomain traffic to the nginx ingress controller
# URL pattern: {service}-{deploymentId}.{cluster_id}.lazycloud.dev (per-cluster wildcard)
# If route_root_domain=true, also routes lazycloud.dev and *.lazycloud.dev
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "cluster_tunnel_config" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel.id

  config = {
    ingress = concat(
      # Root domain routes (only if enabled)
      var.route_root_domain ? [
        {
          hostname = var.cloudflare_zone
          service  = "http://lazycloud-gateway.envoy-gateway-system.svc.cluster.local:80"
        },
        {
          hostname = "*.${var.cloudflare_zone}"
          service  = "http://lazycloud-gateway.envoy-gateway-system.svc.cluster.local:80"
        }
      ] : [],
      # Cluster-specific routes (always)
      [
        {
          hostname = "*.${var.cluster_id}.${var.cloudflare_zone}"
          service  = "http://lazycloud-gateway.envoy-gateway-system.svc.cluster.local:80"
        },
        {
          # Catch-all rule (required by Cloudflare)
          service = "http_status:404"
        }
      ]
    )
  }
}

# Wildcard DNS record pointing to this cluster's tunnel
# Each cluster gets its own subdomain: *.{cluster_id}.lazycloud.dev
resource "cloudflare_dns_record" "cluster_wildcard" {
  zone_id = var.cloudflare_zone_id
  name    = "*.${var.cluster_id}"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel.id}.cfargotunnel.com"
  type    = "CNAME"
  ttl     = 1 # Auto (required for proxied records)
  proxied = true
  comment = "Wildcard DNS for *.${var.cluster_id}.${var.cloudflare_zone} via Cloudflare Tunnel"
}

# Root domain DNS records (only if route_root_domain=true)
# Routes lazycloud.dev and *.lazycloud.dev through this cluster's tunnel
resource "cloudflare_dns_record" "root_domain" {
  count   = var.route_root_domain ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "@"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel.id}.cfargotunnel.com"
  type    = "CNAME"
  ttl     = 1
  proxied = true
  comment = "Root domain ${var.cloudflare_zone} via Cloudflare Tunnel (cluster: ${var.cluster_id})"
}

resource "cloudflare_dns_record" "root_wildcard" {
  count   = var.route_root_domain ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "*"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.cluster_tunnel.id}.cfargotunnel.com"
  type    = "CNAME"
  ttl     = 1
  proxied = true
  comment = "Wildcard DNS for *.${var.cloudflare_zone} via Cloudflare Tunnel (cluster: ${var.cluster_id})"
}

# Advanced Certificate for nested wildcard (*.{cluster_id}.lazycloud.dev)
# Requires Advanced Certificate Manager add-on (~$10/month per zone)
# Let's Encrypt only supports 90-day validity
resource "cloudflare_certificate_pack" "cluster_wildcard" {
  zone_id               = var.cloudflare_zone_id
  type                  = "advanced"
  validation_method     = "txt"
  validity_days         = 90
  certificate_authority = "lets_encrypt"
  hosts = [
    "*.${var.cluster_id}.${var.cloudflare_zone}",
    "${var.cluster_id}.${var.cloudflare_zone}"
  ]

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Store Cluster Secrets in AWS Secrets Manager
# -----------------------------------------------------------------------------
# All cluster-specific secrets are stored in a single JSON object:
#   - kubeconfig: For backend K8s client
#   - cloudflare_tunnel_token: For cloudflared pods via External Secrets
#
# Secret path: lazycloud/clusters/{cluster_id}
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret_version" "cluster" {
  secret_id = aws_secretsmanager_secret.cluster.id
  secret_string = jsonencode({
    kubeconfig               = talos_cluster_kubeconfig.this.kubeconfig_raw
    cloudflare_tunnel_token  = data.cloudflare_zero_trust_tunnel_cloudflared_token.cluster_tunnel_token.token
  })
}

output "tunnel_id" {
  description = "The tunnel the chart's cloudflared runs, through platform-deployment's helm_values. An identifier, not a secret."
  value       = cloudflare_zero_trust_tunnel_cloudflared.public_ingress.id
}

output "tunnel_credentials" {
  description = <<-EOT
    The tunnel's whole identity. platform-deployment copies it into the
    platform secret document, which cloudflared mounts; it exists nowhere
    else once state is discarded.
  EOT
  value = jsonencode({
    AccountTag   = cloudflare_zero_trust_tunnel_cloudflared.public_ingress.account_tag
    TunnelID     = cloudflare_zero_trust_tunnel_cloudflared.public_ingress.id
    TunnelSecret = random_bytes.tunnel_secret.base64
  })
  sensitive = true
}

output "records" {
  description = "The records this module owns, so a reviewer can compare them against the zone."
  value = {
    apex          = cloudflare_dns_record.apex.name
    wildcard      = cloudflare_dns_record.wildcard.name
    hosts         = one(cloudflare_dns_record.hosts[*].name)
    agent_tunnels = cloudflare_dns_record.agent_tunnels.name
    tcp_workloads = cloudflare_dns_record.tcp_workloads.name
  }
}
output "zone_id" {
  description = "Zone identity used by the deployment's release certificate and DNS records."
  value       = var.zone_id
}

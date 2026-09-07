output "tunnel_id" {
  description = "Set as the `tunnel:` value in deploy/public-ingress/cloudflared.yml. An identifier, not a secret."
  value       = cloudflare_zero_trust_tunnel_cloudflared.public_ingress.id
}

output "tunnel_credentials" {
  description = <<-EOT
    Write verbatim to the file LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE names,
    then `chmod 0444` it and `chmod 0700` its directory — the connector runs as
    uid 65532 and reads it directly. This is the whole of the tunnel's identity;
    it exists nowhere else once state is discarded.
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
    apex     = cloudflare_dns_record.apex.name
    wildcard = cloudflare_dns_record.wildcard.name
  }
}
output "release_bucket" {
  description = "R2 bucket for immutable public release artifacts."
  value       = cloudflare_r2_bucket.releases.name
}

output "release_public_url" {
  description = "Public origin for release artifacts and manifests."
  value       = "https://${cloudflare_r2_custom_domain.releases.domain}"
}

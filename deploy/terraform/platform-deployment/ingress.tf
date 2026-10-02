# Public ingress. HTTP reaches the cluster through a locally managed
# Cloudflare tunnel, so the chart's cloudflared ConfigMap is the whole
# routing table. Agents and TCP clients reach the chart's two NLBs directly
# through DNS-only records.

resource "random_bytes" "tunnel_secret" {
  length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "public" {
  account_id    = var.cloudflare_account_id
  name          = var.deployment
  tunnel_secret = random_bytes.tunnel_secret.base64
  config_src    = "local"
}

locals {
  tunnel_target = "${cloudflare_zero_trust_tunnel_cloudflared.public.id}.cfargotunnel.com"
  records = merge(
    {
      apex     = { name = var.domain, content = local.tunnel_target, proxied = true }
      wildcard = { name = "*.${var.domain}", content = local.tunnel_target, proxied = true }
    },
    var.host_load_balancer == null ? {} : { hosts = { name = "hosts.${var.domain}", content = var.host_load_balancer, proxied = false } },
    var.tcp_load_balancer == null ? {} : { tcp = { name = "*.tcp.${var.domain}", content = var.tcp_load_balancer, proxied = false } },
  )
}

resource "cloudflare_dns_record" "platform" {
  for_each = local.records
  zone_id  = var.cloudflare_zone_id
  name     = each.value.name
  type     = "CNAME"
  content  = each.value.content
  proxied  = each.value.proxied
  # Proxied records take ttl 1; Cloudflare owns their real TTL.
  ttl     = each.value.proxied ? 1 : 60
  comment = "LazyCloud ${var.deployment}"
}

# Customer domains are Cloudflare for SaaS custom hostnames that resolve
# through the apex; the edge serves only verified ones.
resource "cloudflare_custom_hostname_fallback_origin" "saas" {
  zone_id = var.cloudflare_zone_id
  origin  = cloudflare_dns_record.platform["apex"].name
}

# The server-hosts NLB terminates agents' TLS with this certificate and
# forwards HTTP/2 to the servers' gRPC port.
resource "aws_acm_certificate" "hosts" {
  domain_name       = "hosts.${var.domain}"
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_dns_record" "hosts_validation" {
  for_each = { for option in aws_acm_certificate.hosts.domain_validation_options : option.domain_name => option }
  zone_id  = var.cloudflare_zone_id
  name     = each.value.resource_record_name
  type     = each.value.resource_record_type
  content  = each.value.resource_record_value
  ttl      = 300
  proxied  = false
}

resource "aws_acm_certificate_validation" "hosts" {
  certificate_arn         = aws_acm_certificate.hosts.arn
  validation_record_fqdns = [for record in cloudflare_dns_record.hosts_validation : record.name]
}

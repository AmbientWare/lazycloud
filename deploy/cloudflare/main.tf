# This module owns three records' worth of a zone, and nothing else.
#
# The zone carries live Google Workspace mail — five MX, an SPF TXT, and a
# site-verification TXT — which survive alongside a proxied apex CNAME only
# because of CNAME flattening. `deploy/AGENTS.md` states the rule plainly: never
# clear the zone, delete records by id. So there is no zone resource here and no
# record set. Each record this module writes is named individually, and anything
# it does not name is invisible to it.

locals {
  # Carried explicitly so adopting these records does not erase the annotation a
  # human left on them. A record this module owns and a record it does not look
  # identical in the dashboard otherwise.
  record_comment = "LazyCloud public ingress -> cloudflared tunnel"
}

resource "random_bytes" "tunnel_secret" {
  # Cloudflare requires at least 32 bytes, base64 encoded. Generated here rather
  # than by hand because the secret is an input to the tunnel, not something the
  # API returns — so whoever creates the tunnel is the only one who ever sees it,
  # and a tunnel whose secret was lost can only be replaced.
  length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "public_ingress" {
  account_id    = var.account_id
  name          = "lazycloud-${var.environment}"
  tunnel_secret = random_bytes.tunnel_secret.base64

  # Locally managed. In this mode Cloudflare pushes no configuration and
  # `deploy/public-ingress/cloudflared.yml` is the only statement of what is
  # exposed; public hostnames added in the dashboard have no effect at all.
  # Switching this to "cloudflare" would silently move ownership of the routing
  # table off this repository.
  config_src = "local"
}

resource "cloudflare_dns_record" "apex" {
  zone_id = var.zone_id
  name    = var.public_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.public_ingress.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1 # Required to be 1 while proxied; Cloudflare owns the real value.
  comment = local.record_comment

  lifecycle {
    precondition {
      condition     = var.confirm_zone_records
      error_message = "Refusing to write zone records until confirm_zone_records is true."
    }
  }
}

resource "cloudflare_dns_record" "wildcard" {
  zone_id = var.zone_id
  name    = "*.${var.public_hostname}"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.public_ingress.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = local.record_comment

  lifecycle {
    precondition {
      condition     = var.confirm_zone_records
      error_message = "Refusing to write zone records until confirm_zone_records is true."
    }
  }
}

# Every active custom hostname resolves through the fallback origin, so this is
# the one resource here that can break somebody else's domain rather than this
# platform's. Off unless asked for, and pointed at the apex — which is what the
# zone already holds, so declaring it should be a no-op that stops it drifting.
resource "cloudflare_custom_hostname_fallback_origin" "saas" {
  count = var.manage_fallback_origin ? 1 : 0

  zone_id = var.zone_id
  origin  = var.public_hostname

  lifecycle {
    precondition {
      condition     = var.confirm_zone_records
      error_message = "Refusing to write the fallback origin until confirm_zone_records is true."
    }
  }
}

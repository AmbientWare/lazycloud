# The tunnel's identity and the zone, read from the cloudflare root that owns
# them. cloudflared reads the credentials from the platform secret document,
# and the records below validate certificates in the same zone.
data "terraform_remote_state" "cloudflare" {
  backend = "s3"

  config = merge(local.terraform_backend_config, { key = var.cloudflare_state_key })
}

locals {
  apex = data.terraform_remote_state.cloudflare.outputs.records.apex
  # Agents dial hosts.<apex>:443. The chart's server-hosts load balancer
  # terminates TLS with this certificate and forwards gRPC to the servers;
  # the cloudflare root points the name at that load balancer.
  hosts_hostname = "hosts.${local.apex}"
}

resource "aws_acm_certificate" "hosts" {
  domain_name       = local.hosts_hostname
  validation_method = "DNS"

  lifecycle { create_before_destroy = true }
}

resource "cloudflare_dns_record" "hosts_certificate" {
  for_each = {
    for option in aws_acm_certificate.hosts.domain_validation_options : option.domain_name => option
  }

  zone_id = data.terraform_remote_state.cloudflare.outputs.zone_id
  name    = each.value.resource_record_name
  type    = each.value.resource_record_type
  content = each.value.resource_record_value
  ttl     = 300
  proxied = false
  comment = "ACM validation for the LazyCloud host connection"
}

resource "aws_acm_certificate_validation" "hosts" {
  certificate_arn         = aws_acm_certificate.hosts.arn
  validation_record_fqdns = [for record in cloudflare_dns_record.hosts_certificate : record.name]
}

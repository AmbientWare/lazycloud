# Two Secrets Manager documents. Terraform writes <deployment>/platform
# whole. <deployment>/operator holds what operators provide (GitHub App,
# Stripe and Resend keys, Cloudflare tokens); it exists before this root and
# outlives it, so Terraform only looks it up and its values never enter
# state. The chart maps each property to the processes that read it.

# Wraps every workspace secret's data key; losing it loses them all.
resource "random_bytes" "secrets_master_key" {
  length = 32

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret" "platform" {
  name = "${var.deployment}/platform"
}

resource "aws_secretsmanager_secret_version" "platform" {
  secret_id = aws_secretsmanager_secret.platform.id
  secret_string = jsonencode({
    LAZYCLOUD_DATABASE_URL          = local.database_url
    LAZYCLOUD_DATABASE_SESSION_URL  = local.session_database_url
    LAZYCLOUD_SECRETS_MASTER_KEY    = random_bytes.secrets_master_key.base64
    LAZYCLOUD_STRIPE_WEBHOOK_SECRET = stripe_webhook_endpoint.billing.secret
    # The tunnel's whole identity, which cloudflared reads as is.
    LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS = jsonencode({
      AccountTag   = var.cloudflare_account_id
      TunnelID     = cloudflare_zero_trust_tunnel_cloudflared.public.id
      TunnelSecret = random_bytes.tunnel_secret.base64
    })
  })
}

data "aws_secretsmanager_secret" "operator" {
  name = "${var.deployment}/operator"
}

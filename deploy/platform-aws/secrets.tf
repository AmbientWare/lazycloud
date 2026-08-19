locals {
  # Every credential the Compose stack reads. Terraform declares the container and
  # the access to it; the values are written by `lazycloud-admin bootstrap publish`
  # or pasted by an operator, never by this configuration. A secret whose value is
  # in Terraform is a secret in the state file.
  runtime_secrets = {
    database-url                  = "PostgreSQL URL, direct connection. PgBouncer breaks the session advisory locks."
    administrator-token           = "Platform administrator bearer, minted by bootstrap."
    worker-token                  = "Shared fleet worker service token."
    worker-capacity-owner         = "Capacity owner id the shared fleet registers against."
    cache-service-token           = "Cache server service token."
    tailnet-oauth-client-id       = "Tailscale OAuth client id, from deploy/tailnet outputs."
    tailnet-oauth-client-secret   = "Tailscale OAuth client secret, from deploy/tailnet outputs."
    cloudflare-api-token          = "Cloudflare token for custom hostnames. Zone SSL and Certificates, edit."
    cloudflare-tunnel-credentials = "Tunnel credentials file contents, from deploy/cloudflare."
    stripe-api-key                = "Stripe restricted key."
    stripe-webhook-secret         = "Stripe webhook signing secret. Returned only at endpoint creation."
    github-client-id              = "GitHub App client id for dashboard sign-in."
    github-client-secret          = "GitHub App client secret."
    telemetry-backend-endpoint    = "OTLP endpoint the collector exports to."
    telemetry-backend-username    = "Telemetry backend basic-auth username."
    telemetry-backend-password    = "Telemetry backend basic-auth password."
    object-store-access-key       = "Empty when the platform role vends S3 access through the SDK chain."
    object-store-secret-key       = "Empty when the platform role vends S3 access through the SDK chain."
  }
}

resource "aws_secretsmanager_secret" "runtime" {
  for_each = local.runtime_secrets

  name        = "${var.deployment}/${each.key}"
  description = each.value

  # Predeployment resets are ordinary, and a 30-day recovery window means a
  # destroyed deployment cannot reuse its own secret names for a month.
  recovery_window_in_days = 0
}

# Which environment variable each secret becomes on the host. The names are the
# ones `compose.yaml` already reads, so a deployment differs from the local stack
# by where the value comes from and not by what it is called.
locals {
  secret_environment = {
    LAZYCLOUD_DATABASE_URL                = aws_secretsmanager_secret.runtime["database-url"].name
    LAZYCLOUD_TOKEN                       = aws_secretsmanager_secret.runtime["administrator-token"].name
    LAZYCLOUD_CACHE_SERVICE_TOKEN         = aws_secretsmanager_secret.runtime["cache-service-token"].name
    LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID     = aws_secretsmanager_secret.runtime["tailnet-oauth-client-id"].name
    LAZYCLOUD_TAILNET_OAUTH_CLIENT_SECRET = aws_secretsmanager_secret.runtime["tailnet-oauth-client-secret"].name
    LAZYCLOUD_CLOUDFLARE_API_TOKEN        = aws_secretsmanager_secret.runtime["cloudflare-api-token"].name
    LAZYCLOUD_STRIPE_API_KEY              = aws_secretsmanager_secret.runtime["stripe-api-key"].name
    LAZYCLOUD_STRIPE_WEBHOOK_SECRET       = aws_secretsmanager_secret.runtime["stripe-webhook-secret"].name
    LAZYCLOUD_GITHUB_CLIENT_ID            = aws_secretsmanager_secret.runtime["github-client-id"].name
    LAZYCLOUD_GITHUB_CLIENT_SECRET        = aws_secretsmanager_secret.runtime["github-client-secret"].name
  }
}

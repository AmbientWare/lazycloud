locals {
  # Every credential this deployment reads, and every one has a writer. Terraform
  # writes the ones it generates or obtains from another module; an operator
  # writes the rest before the first sync, because Secrets Manager has no such
  # thing as a container that exists and answers blank. An empty SecretString
  # is rejected, and a container with no version answers ResourceNotFoundException
  # that fails the whole materialisation rather than the one key. A secret that
  # would be declared here and filled by nobody is one the cluster waits on
  # forever, so it is not declared.
  runtime_secrets = {
    database-url                  = "PostgreSQL URL, direct connection. PgBouncer breaks the session advisory locks."
    administrator-token           = "Platform administrator bearer, minted by bootstrap."
    cache-service-token           = "Cache server service token."
    tailnet-oauth-client-id       = "Tailscale OAuth client id, from deploy/tailnet outputs."
    tailnet-oauth-client-secret   = "Tailscale OAuth client secret, from deploy/tailnet outputs."
    cloudflare-api-token          = "Cloudflare token for custom hostnames. Zone SSL and Certificates, edit."
    cloudflare-tunnel-credentials = "Tunnel credentials file contents, from deploy/cloudflare."
    stripe-api-key                = "Stripe restricted key."
    stripe-webhook-secret         = "Stripe webhook signing secret. Returned only at endpoint creation."
    github-client-id              = "GitHub App client id for dashboard sign-in."
    github-client-secret          = "GitHub App client secret."
    backend-route-auth-key        = "Shared key authenticating backend routes. At least 32 bytes."
    fleet-external-id             = "External ID the platform's own connection role enforces."
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

# Which environment variable each secret becomes in the cluster. These are the
# names the processes read.
#
# Split from `secret_files` below because the chart gives every workload every
# variable named here. A credential only one pod reads does not belong in the
# environment of the four that do not, and a file is how that pod wants it
# anyway.
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
    LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY      = aws_secretsmanager_secret.runtime["backend-route-auth-key"].name
  }

  # Materialised into the same Secret and mounted as a file by the one workload
  # that reads it. `cloudflared` wants a credentials file rather than a value,
  # and it is the whole of the tunnel's identity.
  secret_files = {
    LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS = aws_secretsmanager_secret.runtime["cloudflare-tunnel-credentials"].name
  }
}

# Generated rather than configured, like the fleet external ID. Both authenticate
# one part of this deployment to another and mean nothing outside it, so there is
# nobody to obtain them from and no operator step that could go missing. The
# backend route key is read at 32 bytes minimum and refuses to start below that.
resource "random_password" "shared" {
  for_each = toset(["backend-route-auth-key", "cache-service-token"])

  length  = 64
  special = false
}

resource "aws_secretsmanager_secret_version" "shared" {
  for_each = random_password.shared

  secret_id     = aws_secretsmanager_secret.runtime[each.key].id
  secret_string = each.value.result
}

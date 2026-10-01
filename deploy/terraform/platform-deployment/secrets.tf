# Two Secrets Manager documents per deployment. Terraform writes
# `<deployment>/platform` whole; operators own `<deployment>/operator`
# (GitHub, Stripe, Resend and Cloudflare credentials), whose values never
# enter Terraform state. The chart binds each process to the properties it
# reads (deploy/helm/lazycloud/values.yaml, secretBindings).
locals {
  platform_secret = "${var.deployment}/platform"
  operator_secret = "${var.deployment}/operator"

  platform_values = merge(local.reference_platform_values, {
    LAZYCLOUD_PLATFORM_DATABASE_URL = local.database_url
    # Verbatim: the tunnel's whole identity, which cloudflared reads as is.
    LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS = data.terraform_remote_state.cloudflare.outputs.tunnel_credentials
    LAZYCLOUD_SECRETS_MASTER_KEY            = random_bytes.secrets_master_key.base64
  })
}

# Wraps every workspace secret's data key. Losing it loses every stored
# workspace secret, so it is generated once and never replaced.
resource "random_bytes" "secrets_master_key" {
  length = 32

  lifecycle { prevent_destroy = true }
}

resource "aws_secretsmanager_secret" "platform" {
  name        = local.platform_secret
  description = "Values this configuration generates or reads from another module."

  # Predeployment resets are ordinary, and a 30-day recovery window means a
  # destroyed deployment cannot reuse its own secret names for a month.
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "platform" {
  secret_id     = aws_secretsmanager_secret.platform.id
  secret_string = jsonencode(local.platform_values)
}

resource "aws_secretsmanager_secret" "operator" {
  name        = local.operator_secret
  description = "Operator-managed credentials. Property bindings are declared in the Helm chart."

  recovery_window_in_days = 0
}

# Terraform writes platform values. Operators own their separate document,
# including the tunnel CA and dedicated gateway bootstrap credential.
locals {
  platform_secret = "${var.deployment}/platform"
  operator_secret = "${var.deployment}/operator"

  platform_values = {
    LAZYCLOUD_DATABASE_URL = format(
      "postgresql+psycopg://%s:%s@%s:6432/%s",
      planetscale_postgres_branch_role.control_plane.username,
      planetscale_postgres_branch_role.control_plane.password,
      planetscale_postgres_branch_role.control_plane.access_host_url,
      planetscale_postgres_branch_role.control_plane.database_name,
    )
    LAZYCLOUD_DATABASE_DIRECT_URL = format(
      "postgresql+psycopg://%s:%s@%s:5432/%s",
      planetscale_postgres_branch_role.control_plane.username,
      planetscale_postgres_branch_role.control_plane.password,
      planetscale_postgres_branch_role.control_plane.access_host_url,
      planetscale_postgres_branch_role.control_plane.database_name,
    )
    LAZYCLOUD_CACHE_SERVICE_TOKEN = random_password.cache_service_token.result
    # Verbatim: the whole of the tunnel's identity, which cloudflared reads as-is.
    LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS = data.terraform_remote_state.cloudflare.outputs.tunnel_credentials
    LAZYCLOUD_FLEET_EXTERNAL_ID             = random_password.fleet_external_id.result
  }

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

resource "random_password" "cache_service_token" {
  length  = 64
  special = false
}

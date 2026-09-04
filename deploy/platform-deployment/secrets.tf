# Three documents, and each has exactly one writer.
#
# Secrets Manager bills per entry per month and a deployment holds a dozen
# credentials, so each entry is a JSON document rather than a container of its
# own. The split is by author: Terraform writes what it generates or reads from
# a module that owns it, an operator writes what only a person can obtain, and
# the WireGuard bootstrap writes the key material only it generates.
#
# One document would be cheaper again and cannot work. A document is written
# atomically, so Terraform rendering it would drop every field an operator had
# added. `ignore_changes` is the usual answer and is not available here: the
# database URL carries the PlanetScale role's password and has to be rewritten
# when that rotates, so this configuration cannot be a write-once author.
#
# All are named for the deployment, so a second deployment gets its own entries
# rather than sharing documents with another writer.
locals {
  platform_secret  = "${var.deployment}/platform"
  operator_secret  = "${var.deployment}/operator"
  wireguard_secret = "${var.deployment}/wireguard"

  platform_values = {
    LAZYCLOUD_DATABASE_URL = format(
      "postgresql+psycopg://%s:%s@%s:5432/%s",
      planetscale_postgres_branch_role.control_plane.username,
      planetscale_postgres_branch_role.control_plane.password,
      planetscale_postgres_branch_role.control_plane.access_host_url,
      planetscale_postgres_branch_role.control_plane.database_name,
    )
    LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY = random_password.backend_route_auth_key.result
    LAZYCLOUD_CACHE_SERVICE_TOKEN    = random_password.cache_service_token.result
    # Verbatim: the whole of the tunnel's identity, which cloudflared reads as-is.
    LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS = data.terraform_remote_state.cloudflare.outputs.tunnel_credentials
    LAZYCLOUD_FLEET_EXTERNAL_ID             = random_password.fleet_external_id.result
  }

  # Named here so the runbook and the cluster agree on them. The document itself
  # is written once by an operator; a value in this configuration is a value in
  # the state file.
  operator_variables = {
    LAZYCLOUD_TOKEN                 = "Platform administrator bearer. Write it before the first sync: bootstrap adopts a configured credential and mints an unreachable one when it finds none."
    LAZYCLOUD_GITHUB_CLIENT_ID      = "GitHub App client id for dashboard sign-in."
    LAZYCLOUD_GITHUB_CLIENT_SECRET  = "GitHub App client secret."
    LAZYCLOUD_CLOUDFLARE_API_TOKEN  = "Cloudflare token for custom hostnames. Zone SSL and Certificates, edit."
    LAZYCLOUD_STRIPE_API_KEY        = "Stripe restricted key."
    LAZYCLOUD_STRIPE_WEBHOOK_SECRET = "Stripe webhook signing secret. Returned only at endpoint creation."
    LAZYCLOUD_RESEND_API_KEY        = "Resend API key invitation mail is sent with, read by the scheduler."
    LAZYCLOUD_RESEND_WEBHOOK_SECRET = "Resend webhook signing secret. Returned only at endpoint creation."
  }

  # Which document each variable is read out of. Every workload gets these as
  # environment, which is why the tunnel credential is not among them.
  secret_environment = merge(
    { for name in keys(local.operator_variables) : name => local.operator_secret },
    {
      LAZYCLOUD_DATABASE_URL           = local.platform_secret
      LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY = local.platform_secret
      LAZYCLOUD_CACHE_SERVICE_TOKEN    = local.platform_secret
      # Both halves of this are the module's: the condition on the connection
      # role's trust, and the value the registration presents. Declaring one
      # without giving the other is a role that enforces an ID nothing sends.
      LAZYCLOUD_FLEET_EXTERNAL_ID = local.platform_secret
    },
  )

  # Materialised into the same Kubernetes Secret and mounted as a file by the one
  # workload that reads it. `cloudflared` wants a credentials file rather than a
  # value, and it has no business in the environment of workloads that never open
  # it.
  secret_files = {
    LAZYCLOUD_CLOUDFLARE_TUNNEL_CREDENTIALS = local.platform_secret
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
  name = local.operator_secret
  description = format(
    "Credentials from outside this deployment, as one JSON document. Keys: %s.",
    join(", ", sort(keys(local.operator_variables))),
  )

  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret" "wireguard" {
  name        = local.wireguard_secret
  description = "Durable WireGuard gateway and platform peer keys, initialized by the deployment bootstrap."

  recovery_window_in_days = 0
}

# Generated rather than obtained. Each authenticates one part of this deployment
# to another and means nothing outside it, so there is nobody to get them from
# and no operator step that could go missing. The backend route key is read at 32
# bytes minimum and refuses to start below that.
resource "random_password" "backend_route_auth_key" {
  length  = 64
  special = false
}

resource "random_password" "cache_service_token" {
  length  = 64
  special = false
}

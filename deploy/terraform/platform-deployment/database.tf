# PlanetScale Neki. The branch creates the database with its default
# configuration profile (one shard, a primary and two replicas), router
# group and admin; size them in the dashboard or with
# `pscale size cluster list --engine neki`. Processes connect to the router
# on 5432 over TLS; the router pools backends itself, so there is no
# client-side pooler. The org must have joined the Neki Platform Preview.
resource "planetscale_neki_branch" "main" {
  organization       = var.planetscale_organization
  database           = var.deployment
  name               = "main"
  region             = var.planetscale_region
  deletion_protected = true
}

# The server migrates at start, so its role needs DDL: `postgres`.
resource "planetscale_neki_role" "platform" {
  organization    = var.planetscale_organization
  database        = planetscale_neki_branch.main.database
  branch          = planetscale_neki_branch.main.name
  name            = "platform"
  inherited_roles = ["postgres"]
}

locals {
  database_url = format(
    "postgres://%s:%s@%s:5432/postgres?sslmode=verify-full&pool_max_conns=%d",
    urlencode(planetscale_neki_role.platform.username),
    urlencode(planetscale_neki_role.platform.password),
    planetscale_neki_role.platform.access_host_url,
    var.database_pool_max_connections,
  )
  # LAZYCLOUD_DATABASE_SESSION_URL: LISTEN, the migration, metering and
  # leader locks hold state in their session and need a direct connection.
  # Neki has one endpoint, the router, and the router keeps a session that
  # holds state on one backend until it ends, so both URLs name it; the
  # runbook checks LISTEN and session locks through it before the first
  # release.
  session_database_url = local.database_url
}

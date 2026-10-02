# PlanetScale Neki. The branch creates the database with its default
# configuration profile (one shard, a primary and two replicas), router
# group and admin; size them in the dashboard or with
# `pscale size cluster list --engine neki`. Processes connect to the router
# on 5432 over TLS; the router pools backends itself, so there is no
# PgBouncer. The org must have joined the Neki Platform Preview.
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
}

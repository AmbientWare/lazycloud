# PlanetScale Postgres. Applying the branch creates the database when it
# does not exist, so this one resource is the database. The major version is
# pinned: an upgrade is a decision, not something an apply discovers.
resource "planetscale_postgres_branch" "main" {
  organization       = var.planetscale_organization
  database           = var.deployment
  name               = "main"
  major_version      = "17"
  cluster_size       = var.planetscale_cluster_size
  region             = var.planetscale_region
  deletion_protected = true
}

# The server migrates at start, so its role needs DDL: it inherits
# `postgres` (the API rejects the `pscale_*` names). The role owns the schema
# it migrated; replacing the role (`-replace` rotates its password) hands
# that schema to `postgres` instead of failing on or dropping the owned
# tables.
resource "planetscale_postgres_branch_role" "platform" {
  organization    = var.planetscale_organization
  database        = planetscale_postgres_branch.main.database
  branch          = planetscale_postgres_branch.main.name
  name            = "platform"
  inherited_roles = ["postgres"]
  successor       = "postgres"
}

locals {
  # Both URLs name the direct port 5432, not PgBouncer on 6432: pgx caches
  # prepared statements per connection, which transaction pooling breaks,
  # and LISTEN, the migration, metering and leader locks keep state in their
  # session. Each process opens one pool per URL, so it holds at most twice
  # pool_max_conns backends; the bound keeps every replica within the
  # branch's max_connections.
  database_url = format(
    "postgres://%s:%s@%s:5432/%s?sslmode=verify-full&pool_max_conns=%d",
    urlencode(planetscale_postgres_branch_role.platform.username),
    urlencode(planetscale_postgres_branch_role.platform.password),
    planetscale_postgres_branch_role.platform.access_host_url,
    planetscale_postgres_branch_role.platform.database_name,
    var.database_pool_max_connections,
  )
}

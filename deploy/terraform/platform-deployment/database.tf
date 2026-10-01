# The platform's PostgreSQL: its own database, so the platform starts from a
# fresh schema (migrations/) and the reference platform's database stays
# intact for a rollback (reference.tf).
#
# Processes connect directly on port 5432. LISTEN/NOTIFY wake-ups and
# session advisory locks need a session that stays on one backend, which a
# transaction pooler does not give, so the branch's PgBouncer is not used.
# pool_max_conns in the URL bounds each process's pgx pool instead.
locals {
  platform_database = coalesce(var.platform_database, "${var.deployment}-platform")
}

resource "planetscale_postgres_branch" "platform" {
  organization  = var.planetscale_organization
  database      = local.platform_database
  name          = "main"
  major_version = var.planetscale_major_version
  cluster_size  = var.planetscale_cluster_size
  region        = var.planetscale_region

  parameters = {
    pgconf = {
      max_connections = tostring(var.database_max_connections)
    }
  }
}

# The role the server, scheduler and migrations connect as. A branch role
# inherits nothing by default; `postgres` lets the migrations create the
# schema.
resource "planetscale_postgres_branch_role" "platform" {
  organization    = var.planetscale_organization
  database        = planetscale_postgres_branch.platform.database
  branch          = planetscale_postgres_branch.platform.name
  inherited_roles = ["postgres"]
}

locals {
  database_url = format(
    "postgres://%s:%s@%s:5432/%s?sslmode=verify-full&pool_max_conns=%d",
    urlencode(planetscale_postgres_branch_role.platform.username),
    urlencode(planetscale_postgres_branch_role.platform.password),
    planetscale_postgres_branch_role.platform.access_host_url,
    planetscale_postgres_branch_role.platform.database_name,
    var.database_pool_max_connections,
  )
}

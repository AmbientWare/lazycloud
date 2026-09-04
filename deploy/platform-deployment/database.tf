# The control plane's Postgres.
#
# Applying a branch creates the parent database if it does not exist, so this one
# resource is the database. `major_version` is pinned rather than tracking latest:
# the schema declares `btree_gist` and `pgcrypto` and uses a GiST exclusion
# constraint over `tstzrange` to make overlapping billing rate windows
# impossible, and a major version is not something to discover during an apply.
resource "planetscale_postgres_branch" "control_plane" {
  organization  = var.planetscale_organization
  database      = var.deployment
  name          = "main"
  major_version = var.planetscale_major_version
  cluster_size  = var.planetscale_cluster_size
  region        = var.planetscale_region

  # Export the server ceiling to Helm through the infrastructure descriptor.
  parameters = {
    pgconf = {
      max_connections = tostring(var.database_max_connections)
    }
  }
}

# The role the control plane connects as. Its password exists only here and in
# Secrets Manager.
resource "planetscale_postgres_branch_role" "control_plane" {
  organization = var.planetscale_organization
  database     = planetscale_postgres_branch.control_plane.database
  branch       = planetscale_postgres_branch.control_plane.name

  # Terraform destroys this before the branch it depends on, and PlanetScale
  # refuses a role that still owns the tables the schema created, so a destroy
  # stops here with a 422 and everything else already gone. Deleting the branch
  # takes the role and the database with it, which is why the teardown in
  # LIFECYCLE.md drops this from state first rather than asking Terraform for
  # something the API will not do.

  # A branch role inherits nothing by default, and a role that cannot CREATE in
  # `public` fails on the very first DDL the schema bootstrap issues. The schema
  # also declares `btree_gist` and `pgcrypto`, so this needs to create extensions
  # and not only tables.
  #
  # `postgres`, not `pscale_admin`: the API takes the role to inherit, and the
  # `pscale_*` names that `pg_roles` lists are rejected as invalid values.
  inherited_roles = ["postgres"]
}

# No `planetscale_postgres_bouncer`, and this is not an omission.
#
# PlanetScale's managed PgBouncer runs in transaction pooling mode only, and
# `ControlPlaneRecoveryFence.start_serving` takes `pg_advisory_lock_shared` and
# holds it for the entire lifetime of a serving process. There is no transaction
# to scope that to. Behind a transaction pooler the lock is released when the
# backend is recycled, the fence stops fencing without erroring, and offline
# recovery can mint an administrator credential while replicas are still serving.
# `WorkspaceDeletionFence` has the same shape.
#
# So the control plane connects direct, on `access_host_url`. Adding a bouncer
# here and pointing the URL at it would look like a performance change and behave
# like a correctness one.

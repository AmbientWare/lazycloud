# The database is deliberately not declared here.
#
# PlanetScale's Terraform provider is MySQL-only. At 0.6.1 the resource set is
# `planetscale_database`, `planetscale_branch`, `planetscale_backup`,
# `planetscale_password` and `planetscale_branch_safe_migrations`, and
# `planetscale_database` has no engine selector — its optional arguments are
# Vitess concepts like `migration_framework` and `automatic_migrations`. There is
# no resource that creates a PlanetScale Postgres database.
#
# So the database is created once through the PlanetScale console or API, and the
# only thing that crosses into this configuration is its connection string, which
# an operator writes into the `database-url` secret. `README.md` carries the step.
#
# Two things about that connection string are not free choices:
#
#   * It must be the direct endpoint on 5432, never the PgBouncer one. PlanetScale's
#     managed PgBouncer is transaction-pooling only, and `ControlPlaneRecoveryFence`
#     takes `pg_advisory_lock_shared` and holds it for the lifetime of a serving
#     process. There is no transaction to scope that to. Behind a transaction pooler
#     the lock is released when the backend is recycled, the fence stops fencing
#     without erroring, and offline recovery can mint an administrator credential
#     while replicas are still serving. `WorkspaceDeletionFence` has the same shape.
#
#   * The role it names must be able to `CREATE EXTENSION btree_gist` and `pgcrypto`.
#     `database/tables/base.py` requires both, and the schema cannot be created on
#     any path without them.

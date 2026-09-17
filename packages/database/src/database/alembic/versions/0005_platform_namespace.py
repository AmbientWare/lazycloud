"""Move platform capacity out of customer ownership during a stopped-writer cutover."""

import hashlib
import json

import sqlalchemy as sa
from alembic import context, op
from pydantic import BaseModel, TypeAdapter

revision = "0005_platform_namespace"
down_revision = "0004_image_build_attempts"
branch_labels = None
depends_on = None


class _Network(BaseModel):
    vpc_id: str
    subnet_ids: list[str]
    security_group_id: str


class _Binding(BaseModel):
    id: str
    account_id: str
    role_arn: str
    node_role_arn: str
    node_instance_profile_arn: str
    external_id_sha256: str


def upgrade() -> None:
    connection = op.get_bind()
    op.execute("""
        LOCK TABLE workspaces, compute_units, compute_capacity_operations,
            compute_join_credentials, compute_machine_enrollments, machines, workers,
            provider_node_launches, capacity_recoveries, tokens, worker_cache_generations,
            aws_account_connections, aws_authorization_generations, aws_account_networks
        IN ACCESS EXCLUSIVE MODE
    """)
    expected = TypeAdapter(dict[str, str]).validate_python(
        context.config.attributes.get("platform_bindings", {})
    )
    rows = (
        connection.execute(
            sa.text("""
        SELECT c.id::text, c.account_id, a.authorization_role_arn AS role_arn,
            c.node_role_arn, c.node_instance_profile_arn,
            encode(digest(c.external_id, 'sha256'), 'hex') AS external_id_sha256
        FROM aws_account_connections c
        JOIN aws_authorization_generations a ON a.connection_id = c.id AND a.slot = 'active'
        WHERE c.platform_fleet
    """)
        )
        .mappings()
        .all()
    )
    legacy_count = connection.scalar(
        sa.text("SELECT count(*) FROM aws_account_connections WHERE platform_fleet")
    )
    if legacy_count != len(rows):
        raise RuntimeError("platform adoption requires an active provider authorization")
    for row in rows:
        binding = _Binding.model_validate(dict(row))
        networks = {
            network.region: _Network.model_validate(
                {
                    "vpc_id": network.vpc_id,
                    "subnet_ids": sorted(network.subnet_ids),
                    "security_group_id": network.security_group_id,
                }
            ).model_dump()
            for network in connection.execute(
                sa.text("""
                SELECT region, vpc_id, subnet_ids, security_group_id
                FROM aws_account_networks WHERE connection_id = :id
            """),
                {"id": binding.id},
            )
        }
        identity = binding.model_dump(exclude={"id"}) | {"networks": networks}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        if expected.get(f"aws:{binding.id}") != fingerprint:
            raise RuntimeError("platform adoption does not match the deployment's AWS binding")
    unfinished = connection.scalar(
        sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM aws_account_connections c WHERE c.platform_fleet AND (
                c.phase <> 'ready' OR EXISTS (
                    SELECT 1 FROM aws_authorization_generations a
                    WHERE a.connection_id = c.id AND a.slot <> 'active'
                ) OR EXISTS (
                    SELECT 1 FROM aws_authorization_cleanup_tombstones t
                    WHERE t.connection_id = c.id
                ) OR EXISTS (
                    SELECT 1 FROM compute_units u
                    WHERE u.provider_connection_id = c.id AND NOT u.platform_fleet
                )
            )
        )
    """)
    )
    if unfinished:
        raise RuntimeError(
            "platform authorization or customer capacity must settle before adoption"
        )
    for provider_ref in connection.scalars(
        sa.text(
            "SELECT DISTINCT provider_ref FROM compute_units WHERE platform_fleet AND provider_ref LIKE 'aws:%'"
        )
    ):
        if provider_ref not in expected:
            raise RuntimeError("platform capacity has no matching deployment binding")
    if connection.scalar(
        sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM containers c JOIN machines m
                ON c.machine_id = m.id OR c.runtime_machine_id = m.id::text
            JOIN compute_units u ON u.id::text = m.capacity_owner_id
            WHERE u.platform_fleet AND c.status IN ('pending', 'running')
        )
    """)
    ):
        raise RuntimeError("stop active platform workloads before namespace migration")
    op.add_column(
        "workspaces", sa.Column("kind", sa.String(32), nullable=False, server_default="tenant")
    )
    op.create_check_constraint("ck_workspaces_kind", "workspaces", "kind IN ('tenant', 'platform')")
    op.create_check_constraint(
        "ck_workspaces_platform_namespace",
        "workspaces",
        "kind <> 'platform' OR (status = 'active' AND primary_token_id IS NULL "
        "AND concurrency_limit_id IS NULL AND storage_bucket IS NULL AND storage_credential_key IS NULL)",
    )
    op.create_index(
        "uq_workspaces_platform",
        "workspaces",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("kind = 'platform'"),
    )
    op.alter_column("compute_join_credentials", "user_id", nullable=True)
    op.alter_column("compute_machine_enrollments", "user_id", nullable=True)
    op.execute("""
        INSERT INTO workspaces (id, name, kind, status, signing_key, storage_backend,
            storage_prefix, labels, metadata)
        SELECT id, 'platform-' || id::text, 'platform', 'active',
            'sign_' || encode(gen_random_bytes(32), 'hex'), 'local', '', '{}', '{}'
        FROM (SELECT gen_random_uuid() AS id) identity
        WHERE EXISTS (SELECT 1 FROM compute_units WHERE platform_fleet)
            OR EXISTS (SELECT 1 FROM aws_account_connections WHERE platform_fleet)
    """)
    op.execute("""
        UPDATE tokens t SET status = 'revoked', revoked_at = COALESCE(t.revoked_at, now())
        WHERE t.kind IN ('worker', 'worker-private') AND t.status = 'active' AND EXISTS (
            SELECT 1 FROM workers w JOIN machines m ON m.id = w.machine_id
            JOIN compute_units u ON m.capacity_owner_id = u.id::text
            WHERE u.platform_fleet AND t.worker_id = w.id::text
        )
    """)
    op.execute("""
        UPDATE compute_units SET workspace_id = (SELECT id FROM workspaces WHERE kind = 'platform'),
            provider_connection_id = NULL
        WHERE platform_fleet
    """)
    op.execute("ALTER TABLE capacity_recoveries DISABLE TRIGGER capacity_recovery_ownership_fence")
    for table, owner in (
        ("compute_capacity_operations", "pool_id"),
        ("capacity_recoveries", "source_unit_id"),
        ("provider_node_launches", "unit_id"),
    ):
        op.execute(f"""
            UPDATE {table} r SET workspace_id = u.workspace_id
            FROM compute_units u WHERE u.id = r.{owner} AND u.platform_fleet
        """)
    op.execute("ALTER TABLE capacity_recoveries ENABLE TRIGGER capacity_recovery_ownership_fence")
    for table in ("compute_join_credentials", "compute_machine_enrollments"):
        op.execute(f"""
            UPDATE {table} r SET workspace_id = u.workspace_id, user_id = NULL,
                status = CASE WHEN r.status = 'active' THEN 'revoked' ELSE r.status END,
                revoked_at = CASE WHEN r.status = 'active' THEN COALESCE(r.revoked_at, now()) ELSE r.revoked_at END
            FROM compute_units u WHERE u.id = r.capacity_owner_id AND u.platform_fleet
        """)
    op.execute("""
        UPDATE provider_node_launches l SET revoked_at = COALESCE(l.revoked_at, now()),
            bootstrap_token_ciphertext = NULL
        FROM compute_units u WHERE u.id = l.unit_id AND u.platform_fleet
    """)
    op.execute("""
        UPDATE machines m SET workspace_id = u.workspace_id
        FROM compute_units u WHERE u.id::text = m.capacity_owner_id AND u.platform_fleet
    """)
    op.execute("""
        UPDATE workers w SET workspace_id = m.workspace_id
        FROM machines m JOIN compute_units u ON u.id::text = m.capacity_owner_id
        WHERE w.machine_id = m.id AND u.platform_fleet
    """)
    op.execute("""
        UPDATE worker_cache_generations g SET workspace_id = w.workspace_id
        FROM workers w JOIN machines m ON m.id = w.machine_id
        JOIN compute_units u ON u.id::text = m.capacity_owner_id
        WHERE g.worker_id = w.id::text AND u.platform_fleet
    """)
    op.create_index(
        "uq_compute_machine_enrollments_platform_fingerprint",
        "compute_machine_enrollments",
        ["machine_fingerprint_hash"],
        unique=True,
        postgresql_where=sa.text("user_id IS NULL"),
    )
    op.execute("DELETE FROM aws_account_connections WHERE platform_fleet")
    op.drop_column("aws_account_connections", "platform_fleet")
    op.execute("""
CREATE OR REPLACE FUNCTION protect_platform_namespace()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.kind = 'platform' THEN
            RAISE EXCEPTION 'platform namespace cannot be deleted' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.kind IS DISTINCT FROM OLD.kind THEN
        RAISE EXCEPTION 'namespace ownership is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER platform_namespace_ownership_fence
BEFORE UPDATE OR DELETE ON workspaces
FOR EACH ROW EXECUTE FUNCTION protect_platform_namespace();
CREATE OR REPLACE FUNCTION require_tenant_namespace()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM workspaces WHERE id = NEW.workspace_id AND kind = 'platform') THEN
        RAISE EXCEPTION 'platform namespace cannot have tenant resources' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER workspace_members_tenant_namespace
BEFORE INSERT OR UPDATE OF workspace_id ON workspace_members
FOR EACH ROW EXECUTE FUNCTION require_tenant_namespace();
CREATE TRIGGER workspace_invitations_tenant_namespace
BEFORE INSERT OR UPDATE OF workspace_id ON workspace_invitations
FOR EACH ROW EXECUTE FUNCTION require_tenant_namespace();
    """)

    op.execute("""
CREATE OR REPLACE FUNCTION require_capacity_namespace()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.platform_fleet IS DISTINCT FROM (
        SELECT kind = 'platform' FROM workspaces WHERE id = NEW.workspace_id
    ) THEN
        RAISE EXCEPTION 'capacity ownership must match its namespace' USING ERRCODE = '23514';
    END IF;
    IF NEW.platform_fleet AND NEW.provider_connection_id IS NOT NULL THEN
        RAISE EXCEPTION 'platform capacity cannot use a customer connection' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER compute_units_namespace_ownership
BEFORE INSERT OR UPDATE OF workspace_id, platform_fleet, provider_connection_id ON compute_units
FOR EACH ROW EXECUTE FUNCTION require_capacity_namespace();

CREATE OR REPLACE FUNCTION require_machine_namespace()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM compute_units u JOIN workspaces w ON w.id = u.workspace_id
        WHERE u.id = NEW.capacity_owner_id AND u.workspace_id = NEW.workspace_id
            AND (NEW.user_id IS NULL) = (w.kind = 'platform')
    ) THEN
        RAISE EXCEPTION 'machine ownership must match its capacity namespace' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER compute_join_credentials_namespace_ownership
BEFORE INSERT OR UPDATE OF workspace_id, user_id, capacity_owner_id ON compute_join_credentials
FOR EACH ROW EXECUTE FUNCTION require_machine_namespace();
CREATE TRIGGER compute_machine_enrollments_namespace_ownership
BEFORE INSERT OR UPDATE OF workspace_id, user_id, capacity_owner_id ON compute_machine_enrollments
FOR EACH ROW EXECUTE FUNCTION require_machine_namespace();
    """)


def downgrade() -> None:
    raise RuntimeError("platform ownership requires a forward migration")

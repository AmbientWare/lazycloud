"""Add durable disks with their generations, leases and rates, and credential seeds.

Also drops the Hetzner launch authorization table.
"""

import secrets

import sqlalchemy as sa
from alembic import op

revision = "0017_disks"
down_revision = "0016_deployment_prunes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("credential_secret", sa.String(64), nullable=True))
    connection = op.get_bind()
    for (workspace_id,) in connection.execute(sa.text("SELECT id FROM workspaces")).all():
        connection.execute(
            sa.text("UPDATE workspaces SET credential_secret = :secret WHERE id = :id"),
            {"secret": secrets.token_hex(32), "id": workspace_id},
        )
    op.alter_column("workspaces", "credential_secret", nullable=False)
    op.create_table(
        "disks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.UUID(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(63), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        sa.Column("holder_container_id", sa.UUID(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=False),
        sa.Column("last_worker_id", sa.Text(), nullable=False),
        sa.Column(
            "metered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("metered_bytes", sa.BigInteger(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("volume_state", sa.String(16), nullable=False),
        sa.Column("volume_id", sa.String(64), nullable=False),
        sa.Column("volume_provider_ref", sa.String(160), nullable=False),
        sa.Column("volume_connection_id", sa.UUID(), nullable=True),
        sa.Column("volume_capacity_workspace_id", sa.Text(), nullable=False),
        sa.Column("volume_region", sa.Text(), nullable=False),
        sa.Column("volume_zone", sa.Text(), nullable=False),
        sa.Column("volume_instance_id", sa.String(64), nullable=False),
        sa.Column("volume_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("volume_token", sa.String(64), nullable=False),
        sa.Column("volume_formatted", sa.Boolean(), nullable=False),
        sa.Column("volume_revision", sa.BigInteger(), nullable=False),
        sa.Column("volume_driver", sa.String(64), nullable=False),
        sa.Column("volume_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "volume_state IN ('none', 'creating', 'attaching', 'attached', 'releasing', "
            "'detaching', 'cached', 'deleting')",
            name="ck_disks_volume_state",
        ),
        sa.CheckConstraint(
            "volume_state = 'none' OR (volume_provider_ref <> '' AND volume_region <> '' "
            "AND volume_zone <> '' AND volume_size_bytes > 0 AND volume_token <> '' "
            "AND volume_capacity_workspace_id <> '' AND volume_driver <> '' "
            "AND volume_changed_at IS NOT NULL)",
            name="ck_disks_volume_scope",
        ),
        sa.CheckConstraint(
            "volume_state IN ('none', 'creating') OR volume_id <> ''",
            name="ck_disks_volume_id",
        ),
        sa.CheckConstraint(
            "volume_state NOT IN ('creating', 'attaching', 'attached', 'releasing', 'detaching') "
            "OR volume_instance_id <> ''",
            name="ck_disks_volume_instance",
        ),
        sa.CheckConstraint("size_bytes > 0", name="ck_disks_size_positive"),
        sa.CheckConstraint("generation >= 0", name="ck_disks_generation_nonnegative"),
        sa.CheckConstraint("stored_bytes >= 0", name="ck_disks_stored_bytes_nonnegative"),
        sa.CheckConstraint("metered_bytes >= 0", name="ck_disks_metered_bytes_nonnegative"),
        sa.CheckConstraint(
            "(holder_container_id IS NULL) = (lease_token = '')",
            name="ck_disks_holder_has_lease",
        ),
        sa.CheckConstraint(
            "(status = 'deleting' AND deleted_at IS NOT NULL AND holder_container_id IS NULL) "
            "OR (status = 'attached' AND deleted_at IS NULL AND holder_container_id IS NOT NULL) "
            "OR (status = 'detached' AND deleted_at IS NULL AND holder_container_id IS NULL)",
            name="ck_disks_status",
        ),
    )
    op.create_index(
        "uq_disks_workspace_name_live",
        "disks",
        ["workspace_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_disks_metered_at_live",
        "disks",
        ["metered_at", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_disks_deleting",
        "disks",
        ["deleted_at", "id"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )
    op.create_index("ix_disks_holder", "disks", ["holder_container_id"])
    op.create_index(
        "ix_disks_volume_due",
        "disks",
        ["volume_changed_at", "id"],
        postgresql_where=sa.text(
            "volume_state IN ('creating', 'attaching', 'attached', 'releasing', 'detaching', "
            "'cached', 'deleting')"
        ),
    )
    op.create_index(
        "ix_disks_volume_connection",
        "disks",
        ["volume_connection_id"],
        postgresql_where=sa.text("volume_connection_id IS NOT NULL"),
    )
    op.create_table(
        "disk_generations",
        sa.Column(
            "disk_id",
            sa.UUID(),
            sa.ForeignKey("disks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("generation", sa.BigInteger(), primary_key=True),
        sa.Column("parent_generation", sa.BigInteger(), nullable=False),
        sa.Column("manifest_key", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("stored_bytes_added", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("generation > 0", name="ck_disk_generations_generation_positive"),
        sa.CheckConstraint(
            "parent_generation >= 0 AND parent_generation < generation",
            name="ck_disk_generations_parent_before",
        ),
        sa.CheckConstraint(
            "stored_bytes_added >= 0", name="ck_disk_generations_stored_bytes_nonnegative"
        ),
    )
    op.add_column(
        "containers",
        sa.Column(
            "scheduling_preferred_worker_id",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "containers",
        sa.Column("scheduling_disk_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "containers",
        sa.Column("scheduling_disk_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "containers",
        sa.Column(
            "scheduling_preferred_availability_zone",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.create_check_constraint(
        "ck_containers_scheduling_disks",
        "containers",
        "scheduling_disk_bytes >= 0 AND scheduling_disk_count >= 0",
    )
    op.drop_constraint("ck_containers_termination_reason", "containers", type_="check")
    op.create_check_constraint(
        "ck_containers_termination_reason",
        "containers",
        "termination_reason IN ('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', "
        "'MEMORY_EVICTED', 'DISK_FULL', 'UNKNOWN')",
    )
    op.create_table(
        "disk_attachments",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "disk_id",
            sa.UUID(),
            sa.ForeignKey("disks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("container_id", sa.UUID(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("size_bytes > 0", name="ck_disk_attachments_size_positive"),
        sa.CheckConstraint(
            "released_at IS NULL OR released_at >= acquired_at",
            name="ck_disk_attachments_release_after_acquire",
        ),
        sa.CheckConstraint(
            "metered_at >= acquired_at", name="ck_disk_attachments_metered_after_acquire"
        ),
        sa.CheckConstraint(
            "settled_at IS NULL OR released_at IS NOT NULL",
            name="ck_disk_attachments_settled_released",
        ),
    )
    op.create_index(
        "uq_disk_attachments_open",
        "disk_attachments",
        ["disk_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index(
        "ix_disk_attachments_unsettled",
        "disk_attachments",
        ["metered_at", "id"],
        postgresql_where=sa.text("settled_at IS NULL"),
    )
    op.execute("""
CREATE TABLE billing_disk_rates (
	pricing_version VARCHAR(64) NOT NULL,
	effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	nanos_per_stored_byte_second NUMERIC(30, 12) NOT NULL,
	nanos_per_attached_byte_second NUMERIC(30, 12) NOT NULL,
	id UUID DEFAULT gen_random_uuid() NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ex_billing_disk_rates_window EXCLUDE USING gist (tstzrange(effective_at, valid_until, '[)') WITH &&),
	CONSTRAINT uq_billing_disk_rates_start UNIQUE (effective_at),
	CONSTRAINT ck_billing_disk_rates_window CHECK (valid_until IS NULL OR valid_until > effective_at),
	CONSTRAINT ck_billing_disk_rates_nonnegative CHECK (nanos_per_stored_byte_second >= 0 AND nanos_per_attached_byte_second >= 0)
)
""")
    op.execute("CREATE INDEX ix_billing_disk_rates_lookup ON billing_disk_rates (effective_at)")
    op.drop_constraint(
        "ck_billing_ledger_segments_dimension", "billing_ledger_segments", type_="check"
    )
    op.create_check_constraint(
        "ck_billing_ledger_segments_dimension",
        "billing_ledger_segments",
        "dimension IN ('compute_runtime', 'network_egress', 'volume_storage', 'disk')",
    )
    op.drop_constraint(
        "ck_billing_ledger_segments_component", "billing_ledger_segments", type_="check"
    )
    op.create_check_constraint(
        "ck_billing_ledger_segments_component",
        "billing_ledger_segments",
        "component IN ('container_time', 'cpu', 'memory', 'gpu', 'egress', "
        "'volume_storage', 'disk_storage', 'disk_attached')",
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM compute_units "
        "WHERE provider_ref LIKE 'hetzner:%' AND phase <> 'deleted') "
        "THEN RAISE EXCEPTION 'delete every Hetzner compute unit before upgrading'; "
        "END IF; END $$"
    )
    op.drop_table("provider_node_launches")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM disks) THEN RAISE EXCEPTION 'delete every disk before downgrading'; END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM billing_ledger_segments WHERE dimension = 'disk') "
        "THEN RAISE EXCEPTION 'disk charges are on the ledger; they cannot be downgraded'; "
        "END IF; END $$"
    )
    op.drop_constraint(
        "ck_billing_ledger_segments_component", "billing_ledger_segments", type_="check"
    )
    op.create_check_constraint(
        "ck_billing_ledger_segments_component",
        "billing_ledger_segments",
        "component IN ('container_time', 'cpu', 'memory', 'gpu', 'egress', 'volume_storage')",
    )
    op.drop_constraint(
        "ck_billing_ledger_segments_dimension", "billing_ledger_segments", type_="check"
    )
    op.create_check_constraint(
        "ck_billing_ledger_segments_dimension",
        "billing_ledger_segments",
        "dimension IN ('compute_runtime', 'network_egress', 'volume_storage')",
    )
    op.drop_table("billing_disk_rates")
    op.drop_table("disk_attachments")
    op.execute(
        "UPDATE containers SET termination_reason = 'UNKNOWN' "
        "WHERE termination_reason = 'DISK_FULL'"
    )
    op.drop_constraint("ck_containers_termination_reason", "containers", type_="check")
    op.create_check_constraint(
        "ck_containers_termination_reason",
        "containers",
        "termination_reason IN ('TTL', 'USER', 'SCHEDULER', 'PREEMPTED', 'ADMIN', 'UNFUNDED', "
        "'MEMORY_EVICTED', 'UNKNOWN')",
    )
    op.drop_constraint("ck_containers_scheduling_disks", "containers", type_="check")
    op.drop_column("containers", "scheduling_preferred_availability_zone")
    op.drop_column("containers", "scheduling_disk_count")
    op.drop_column("containers", "scheduling_disk_bytes")
    op.drop_column("containers", "scheduling_preferred_worker_id")
    op.drop_table("disk_generations")
    op.drop_table("disks")
    op.drop_column("workspaces", "credential_secret")
    op.execute("""
CREATE TABLE provider_node_launches (
	id UUID NOT NULL,
	workspace_id UUID NOT NULL,
	unit_id UUID NOT NULL,
	provider_ref VARCHAR(255) NOT NULL,
	region VARCHAR(64) NOT NULL,
	generation INTEGER NOT NULL,
	server_name VARCHAR(255) NOT NULL,
	provider_instance_id VARCHAR(128),
	bootstrap_token_hash VARCHAR(64) NOT NULL,
	bootstrap_token_ciphertext TEXT,
	node_token_hash VARCHAR(64),
	fingerprint_hash VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	redeemed_at TIMESTAMP WITH TIME ZONE,
	revoked_at TIMESTAMP WITH TIME ZONE,
	enrolled_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_provider_node_launches_expiry CHECK (expires_at > created_at),
	CONSTRAINT ck_provider_node_launches_generation CHECK (generation > 0),
	CONSTRAINT ck_provider_node_launches_redemption CHECK ((redeemed_at IS NULL AND node_token_hash IS NULL) OR (redeemed_at IS NOT NULL AND node_token_hash IS NOT NULL AND bootstrap_token_ciphertext IS NULL)),
	FOREIGN KEY(workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
	FOREIGN KEY(unit_id) REFERENCES compute_units (id) ON DELETE CASCADE
)
""")
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_node_launches_active_instance ON provider_node_launches "
        "(provider_ref, provider_instance_id) "
        "WHERE revoked_at IS NULL AND provider_instance_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_node_launches_active_slot ON provider_node_launches "
        "(unit_id, server_name) WHERE revoked_at IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_node_launches_node_hash "
        "ON provider_node_launches (node_token_hash)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_provider_node_launches_token_hash "
        "ON provider_node_launches (bootstrap_token_hash)"
    )

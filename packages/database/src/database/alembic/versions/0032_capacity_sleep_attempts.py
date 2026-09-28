"""Separate each sleep attempt's evidence from activation restoration."""

import sqlalchemy as sa
from alembic import op

revision = "0032_capacity_sleep_attempts"
down_revision = "0031_execution_entry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_compute_provider_instances_live", table_name="compute_provider_instances")
    op.create_index(
        "ix_compute_provider_instances_live",
        "compute_provider_instances",
        ["pool_id"],
        postgresql_where=sa.text(
            "status NOT IN ('deleted', 'failed') OR provider_storage_destroyed_at IS NULL"
        ),
    )
    op.create_table(
        "capacity_sleep_attempts",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column(
            "instance_record_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("compute_provider_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("boot_id", sa.Text(), nullable=False),
        sa.Column("requested_mode", sa.Text(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("marker_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_mode", sa.Text(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("image_evidence", sa.Text(), nullable=False),
        sa.Column("evidence_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("instance_record_id", "id", name="uq_capacity_sleep_instance_attempt"),
        sa.CheckConstraint(
            "requested_mode IS NULL OR requested_mode IN ('stop', 'hibernate')",
            name="ck_capacity_sleep_requested_mode",
        ),
        sa.CheckConstraint(
            "accepted_mode IS NULL OR accepted_mode IN ('stop', 'hibernate')",
            name="ck_capacity_sleep_accepted_mode",
        ),
        sa.CheckConstraint(
            "(accepted_mode IS NULL) = (accepted_at IS NULL)", name="ck_capacity_sleep_acceptance"
        ),
        sa.CheckConstraint(
            "superseded_at IS NULL OR (superseded_at >= observed_at "
            "AND (accepted_at IS NULL OR superseded_at >= accepted_at))",
            name="ck_capacity_sleep_superseded_at",
        ),
        sa.CheckConstraint(
            "image_evidence IN ('unknown', 'saved', 'failed', 'unavailable')",
            name="ck_capacity_sleep_image_evidence",
        ),
        sa.CheckConstraint(
            "image_evidence != 'saved' OR (boot_id != '' AND provider_stopped_at IS NOT NULL "
            "AND coalesce(accepted_mode, requested_mode, '') = 'hibernate' "
            "AND marker_observed_at IS NOT NULL AND evidence_at IS NOT NULL)",
            name="ck_capacity_sleep_saved_evidence",
        ),
        sa.CheckConstraint(
            "evidence_reason IS NULL OR evidence_reason IN ('save_completed', 'save_failed', "
            "'unsupported', 'plain_stop', 'evidence_missing', 'evidence_expired', "
            "'external_change', 'legacy_unknown', 'provider_rejected', 'forced_stop', 'save_aborted')",
            name="ck_capacity_sleep_reason",
        ),
    )
    op.add_column(
        "compute_provider_instances",
        sa.Column("current_sleep_attempt_id", sa.Uuid(as_uuid=False), nullable=True),
    )
    op.create_foreign_key(
        "fk_provider_instance_current_sleep",
        "compute_provider_instances",
        "capacity_sleep_attempts",
        ["id", "current_sleep_attempt_id"],
        ["instance_record_id", "id"],
    )
    op.execute("""
        INSERT INTO capacity_sleep_attempts (
            id, instance_record_id, boot_id, requested_mode, observed_at,
            provider_stopped_at, image_evidence, evidence_reason
        )
        SELECT gen_random_uuid(), id, '', stop_mode, updated_at,
               CASE WHEN status = 'stopped' THEN updated_at END,
               'unknown', 'legacy_unknown'
        FROM compute_provider_instances
        WHERE status IN ('stopping', 'stopped', 'resuming')
    """)
    op.execute("""
        UPDATE compute_provider_instances i
        SET current_sleep_attempt_id = s.id
        FROM capacity_sleep_attempts s WHERE s.instance_record_id = i.id
    """)
    op.execute("""
        UPDATE capacity_sleep_attempts s
        SET accepted_mode = slot->>'stop_mode',
            accepted_at = (slot->>'stop_requested_at')::timestamptz
        FROM compute_provider_instances i
        JOIN compute_units u ON u.id = i.pool_id
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(u.provider_attributes->'slots') = 'array'
                 THEN u.provider_attributes->'slots' ELSE '[]'::jsonb END
        ) AS slot
        WHERE s.instance_record_id = i.id
          AND slot->>'instance_id' = i.instance_id
          AND slot->>'stop_mode' IN ('stop', 'hibernate')
          AND slot->>'stop_requested_at' IS NOT NULL
          AND u.provider_resource_id LIKE 'ec2-pool-%'
    """)
    op.execute("""
        UPDATE compute_units u
        SET provider_attributes = jsonb_set(provider_attributes, '{slots}', (
            SELECT COALESCE(jsonb_agg(
                (slot - 'hibernate' - 'stop_mode' - 'stop_requested_at') ||
                CASE WHEN slot->>'stop_mode' IN ('stop', 'hibernate')
                           AND slot->>'stop_requested_at' IS NOT NULL
                     THEN jsonb_build_object('sleep_accepted_mode', slot->'stop_mode',
                                             'sleep_accepted_at', slot->'stop_requested_at')
                     ELSE '{}'::jsonb END
                ORDER BY ordinal), '[]'::jsonb)
            FROM jsonb_array_elements(u.provider_attributes->'slots')
                 WITH ORDINALITY AS entries(slot, ordinal)
        )), provider_state_revision = provider_state_revision + 1
        WHERE provider_resource_id LIKE 'ec2-pool-%'
          AND jsonb_typeof(provider_attributes->'slots') = 'array'
    """)
    op.drop_constraint("ck_compute_provider_instances_sleep_outcome", "compute_provider_instances")
    op.drop_constraint("ck_compute_provider_instances_sleep_mode", "compute_provider_instances")
    op.drop_column("compute_provider_instances", "sleep_outcome")
    op.drop_column("compute_provider_instances", "stop_mode")

    op.drop_constraint("ck_capacity_activation_sleep_outcome", "capacity_activations")
    op.alter_column("capacity_activations", "sleep_outcome", new_column_name="restore_outcome")
    op.execute("""
        UPDATE capacity_activations SET restore_outcome = CASE restore_outcome
            WHEN 'hibernated' THEN 'memory_restored'
            WHEN 'stopped' THEN 'cold_boot' ELSE 'unknown' END
    """)
    op.create_check_constraint(
        "ck_capacity_activation_restore_outcome",
        "capacity_activations",
        "restore_outcome IN ('unknown', 'cold_boot', 'memory_restored')",
    )
    op.add_column(
        "capacity_activations",
        sa.Column("restore_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE capacity_activations SET restore_observed_at = authorized_at WHERE restore_outcome != 'unknown'"
    )
    op.add_column(
        "capacity_activations", sa.Column("sleep_attempt_id", sa.Uuid(as_uuid=False), nullable=True)
    )
    op.create_foreign_key(
        "fk_capacity_activation_sleep",
        "capacity_activations",
        "capacity_sleep_attempts",
        ["instance_record_id", "sleep_attempt_id"],
        ["instance_record_id", "id"],
    )
    op.execute("""
        UPDATE capacity_activations a
        SET sleep_attempt_id = i.current_sleep_attempt_id
        FROM compute_provider_instances i
        WHERE i.id = a.instance_record_id
          AND i.status = 'resuming'
          AND i.current_sleep_attempt_id IS NOT NULL
          AND a.requested_at = i.activation_requested_at
          AND a.kind IN ('boot', 'resume')
          AND a.prepared_at IS NULL
          AND a.failed_at IS NULL
    """)
    op.create_index(
        "ix_capacity_activations_sleep",
        "capacity_activations",
        ["sleep_attempt_id"],
        postgresql_where=sa.text("sleep_attempt_id IS NOT NULL"),
    )
    op.add_column(
        "capacity_activations",
        sa.Column(
            "id",
            sa.Uuid(as_uuid=False),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    op.add_column(
        "capacity_activations", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE capacity_activations SET observed_at = requested_at")
    op.alter_column("capacity_activations", "observed_at", nullable=False)
    op.drop_constraint("capacity_activations_pkey", "capacity_activations", type_="primary")
    op.create_primary_key("capacity_activations_pkey", "capacity_activations", ["id"])
    op.alter_column("capacity_activations", "requested_at", nullable=True)
    op.create_unique_constraint(
        "uq_capacity_activation_request",
        "capacity_activations",
        ["instance_record_id", "requested_at"],
    )
    op.create_index(
        "uq_capacity_activation_external_sleep",
        "capacity_activations",
        ["instance_record_id", "sleep_attempt_id"],
        unique=True,
        postgresql_where=sa.text("requested_at IS NULL"),
    )
    op.create_check_constraint(
        "ck_capacity_activation_external_sleep",
        "capacity_activations",
        "requested_at IS NOT NULL OR sleep_attempt_id IS NOT NULL",
    )


def downgrade() -> None:
    # An external activation has no request instant. Restoring the old primary key
    # would require inventing one or deleting history.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM capacity_activations WHERE requested_at IS NULL) THEN
                RAISE EXCEPTION 'cannot downgrade activation history with unknown request times';
            END IF;
        END $$;
    """)
    op.drop_constraint("ck_capacity_activation_external_sleep", "capacity_activations")
    op.drop_index("uq_capacity_activation_external_sleep", table_name="capacity_activations")
    op.drop_constraint("uq_capacity_activation_request", "capacity_activations", type_="unique")
    op.drop_constraint("capacity_activations_pkey", "capacity_activations", type_="primary")
    op.alter_column("capacity_activations", "requested_at", nullable=False)
    op.create_primary_key(
        "capacity_activations_pkey", "capacity_activations", ["instance_record_id", "requested_at"]
    )
    op.drop_column("capacity_activations", "observed_at")
    op.drop_column("capacity_activations", "id")
    op.drop_index("ix_compute_provider_instances_live", table_name="compute_provider_instances")
    op.create_index(
        "ix_compute_provider_instances_live",
        "compute_provider_instances",
        ["pool_id"],
        postgresql_where=sa.text("status NOT IN ('deleted', 'failed')"),
    )
    op.drop_constraint("fk_capacity_activation_sleep", "capacity_activations", type_="foreignkey")
    op.drop_index("ix_capacity_activations_sleep", table_name="capacity_activations")
    op.drop_column("capacity_activations", "sleep_attempt_id")
    op.drop_column("capacity_activations", "restore_observed_at")
    op.drop_constraint("ck_capacity_activation_restore_outcome", "capacity_activations")
    op.alter_column("capacity_activations", "restore_outcome", new_column_name="sleep_outcome")
    op.execute("""
        UPDATE capacity_activations SET sleep_outcome = CASE sleep_outcome
            WHEN 'memory_restored' THEN 'hibernated'
            WHEN 'cold_boot' THEN 'stopped' ELSE 'unknown' END
    """)
    op.create_check_constraint(
        "ck_capacity_activation_sleep_outcome",
        "capacity_activations",
        "sleep_outcome IN ('unknown', 'stopped', 'hibernated')",
    )
    op.add_column("compute_provider_instances", sa.Column("stop_mode", sa.Text(), nullable=True))
    op.add_column(
        "compute_provider_instances",
        sa.Column("sleep_outcome", sa.Text(), nullable=False, server_default=sa.text("'unknown'")),
    )
    op.execute("""
        UPDATE compute_provider_instances i SET stop_mode = s.requested_mode
        FROM capacity_sleep_attempts s WHERE s.id = i.current_sleep_attempt_id
    """)
    op.create_check_constraint(
        "ck_compute_provider_instances_sleep_mode",
        "compute_provider_instances",
        "stop_mode IS NULL OR stop_mode IN ('stop', 'hibernate')",
    )
    op.create_check_constraint(
        "ck_compute_provider_instances_sleep_outcome",
        "compute_provider_instances",
        "sleep_outcome IN ('unknown', 'stopped', 'hibernated')",
    )
    op.drop_constraint(
        "fk_provider_instance_current_sleep", "compute_provider_instances", type_="foreignkey"
    )
    op.drop_column("compute_provider_instances", "current_sleep_attempt_id")
    op.drop_table("capacity_sleep_attempts")

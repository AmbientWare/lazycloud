"""Give stored artifacts a finite plan window and remove retention overrides."""

from alembic import op

revision = "0031_artifact_plan_retention"
down_revision = "0030_withdraw_rate_schedule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Freeze the rollout policy here: later catalog changes must not rewrite
    # the grace window a deployed installation gave its existing files.
    op.execute("""
        WITH plan_windows AS (
            SELECT w.id AS workspace_id,
                CASE
                    WHEN b.complimentary_since IS NOT NULL THEN 2592000
                    WHEN b.plan = 'business' THEN 7776000
                    WHEN b.plan = 'team' THEN 2592000
                    ELSE 86400
                END AS seconds
            FROM workspaces w
            LEFT JOIN workspace_members m ON m.workspace_id = w.id AND m.role = 'owner'
            LEFT JOIN billing_accounts b ON b.user_id = m.user_id
        ), expirations AS (
            SELECT o.id, p.seconds,
                LEAST(o.artifact_expires_at, now() + p.seconds * interval '1 second') AS expires_at
            FROM objects o
            JOIN plan_windows p ON p.workspace_id = o.workspace_id
            WHERE o.artifact_task_id IS NOT NULL
        )
        UPDATE objects o
        SET artifact_expires_at = e.expires_at,
            payload = o.payload::jsonb || jsonb_build_object(
                'artifact_retention_seconds', e.seconds,
                'artifact_expires_at', e.expires_at
            ) || CASE WHEN o.payload->'write_target'->>'artifact_task_id' IS NOT NULL
                THEN jsonb_build_object('write_target', o.payload::jsonb->'write_target'
                    || jsonb_build_object('artifact_retention_seconds', e.seconds))
                ELSE '{}'::jsonb END
        FROM expirations e
        WHERE o.id = e.id
    """)
    op.execute("""
        UPDATE objects SET payload = payload::jsonb - 'artifact_retention_source'
        WHERE payload::jsonb ? 'artifact_retention_source'
    """)
    op.execute("""
        UPDATE objects SET payload = jsonb_set(
            payload::jsonb, '{write_target}',
            (payload::jsonb->'write_target') - 'artifact_retention_source'
        )
        WHERE payload::jsonb->'write_target' ? 'artifact_retention_source'
    """)
    op.create_check_constraint(
        "ck_objects_artifact_expiration_required",
        "objects",
        "artifact_task_id IS NULL OR write_claimed_at IS NOT NULL "
        "OR artifact_expires_at IS NOT NULL",
    )
    op.drop_table("artifact_retention")


def downgrade() -> None:
    raise RuntimeError("artifact expiration commitments cannot be discarded")

"""Preserve connected AWS networks under their regional identity."""

from alembic import op

revision = "0032_aws_regional_networks"
down_revision = "0031_artifact_plan_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing-role connections used the fixed us-east-1 validator. Managed
    # connections record the region of the stack that created their network.
    op.execute("""
        UPDATE aws_account_connections
        SET payload = (payload::jsonb - 'network') || jsonb_build_object(
            'networks', CASE
                WHEN payload->'network' IS NULL OR payload->'network' = 'null'::jsonb
                    THEN '{}'::jsonb
                ELSE jsonb_build_object(
                    COALESCE(
                        payload #>> '{active_authorization,managed_authorization,region}',
                        payload #>> '{pending_authorization,managed_authorization,region}',
                        'us-east-1'
                    ), payload->'network'
                )
            END
        )
    """)


def downgrade() -> None:
    raise RuntimeError(
        "AWS regional networks cannot be downgraded without losing network ownership"
    )

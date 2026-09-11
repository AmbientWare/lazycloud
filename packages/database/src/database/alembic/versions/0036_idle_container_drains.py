"""Allow idle containers to retire without a replacement capacity promise."""

from alembic import op

revision = "0036_idle_container_drains"
down_revision = "0035_device_login_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_container_rollout_drains_serving_floor", "container_rollout_drains", type_="check"
    )
    op.create_check_constraint(
        "ck_container_rollout_drains_serving_floor",
        "container_rollout_drains",
        "serving_floor >= 0",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM container_rollout_drains WHERE serving_floor = 0
            ) THEN
                RAISE EXCEPTION 'Cannot downgrade while idle retirement records exist';
            END IF;
        END $$
    """)
    op.drop_constraint(
        "ck_container_rollout_drains_serving_floor", "container_rollout_drains", type_="check"
    )
    op.create_check_constraint(
        "ck_container_rollout_drains_serving_floor", "container_rollout_drains", "serving_floor > 0"
    )

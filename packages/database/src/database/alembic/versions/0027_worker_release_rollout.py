"""Track the release owning temporary update capacity."""

import sqlalchemy as sa
from alembic import op

revision = "0027_worker_release_rollout"
down_revision = "0026_reserve_resume_authorized"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workers", sa.Column("update_error", sa.String(512), nullable=False, server_default="")
    )
    op.add_column(
        "compute_units",
        sa.Column(
            "replacement_release_generation", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "compute_units",
        sa.Column("replacement_reason", sa.String(512), nullable=False, server_default=""),
    )
    op.create_check_constraint(
        "ck_compute_units_release_replacement",
        "compute_units",
        "replacement_release_generation >= 0 AND "
        "(replacement_release_generation = 0 OR "
        "(replacement_machine_id <> '' AND replacement_template_version = ''))",
    )
    op.create_index(
        "ix_compute_units_release_replacement",
        "compute_units",
        ["id"],
        postgresql_where=sa.text("replacement_release_generation > 0"),
    )


def downgrade() -> None:
    op.drop_column("workers", "update_error")
    op.drop_index("ix_compute_units_release_replacement", table_name="compute_units")
    op.drop_constraint("ck_compute_units_release_replacement", "compute_units", type_="check")
    op.drop_column("compute_units", "replacement_reason")
    op.drop_column("compute_units", "replacement_release_generation")

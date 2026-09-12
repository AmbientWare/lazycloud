"""Identify prepared execution revisions without rewriting installed stubs."""

import sqlalchemy as sa
from alembic import op

revision = "0044_stub_preparation_identity"
down_revision = "0043_wireguard_gateway_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing payloads may disagree with the containers started from them.
    op.add_column("stubs", sa.Column("preparation_fingerprint", sa.String(64), nullable=True))
    op.create_unique_constraint(
        "uq_stubs_preparation_fingerprint", "stubs", ["workspace_id", "preparation_fingerprint"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_stubs_preparation_fingerprint", "stubs")
    op.drop_column("stubs", "preparation_fingerprint")

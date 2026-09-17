"""Track direct object uploads on their durable write claims."""

import sqlalchemy as sa
from alembic import op

revision = "0006_object_uploads"
down_revision = "0005_platform_namespace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "objects", sa.Column("write_upload_id", sa.Text(), nullable=False, server_default="")
    )
    op.create_check_constraint(
        "ck_objects_upload_claim", "objects", "write_upload_id = '' OR write_claimed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint("ck_objects_upload_claim", "objects", type_="check")
    op.drop_column("objects", "write_upload_id")

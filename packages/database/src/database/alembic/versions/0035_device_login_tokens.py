"""Record device-login origin independently of token authority."""

from alembic import op

revision = "0035_device_login_tokens"
down_revision = "0034_remove_reload_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tokens ADD COLUMN device_login BOOLEAN NOT NULL DEFAULT false")
    # Device-code records expire, so older CLI credentials can only be identified
    # by the CLI's default name. New credentials record their origin when issued.
    op.execute("""
        UPDATE tokens SET device_login = true
        WHERE kind = 'user' AND (name = 'cli' OR name LIKE 'cli@%')
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE tokens DROP COLUMN device_login")

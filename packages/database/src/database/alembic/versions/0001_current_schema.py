from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from database.tables import DatabaseBase

# This identifier must change whenever the predeployment baseline changes so an
# older disposable database cannot be mistaken for the current schema.
revision = "20260731_pool_bootstrap_credentials"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    DatabaseBase.metadata.create_all(connection)

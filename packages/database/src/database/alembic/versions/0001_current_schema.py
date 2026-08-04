from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from database.tables import DatabaseBase

# This identifier must change whenever the predeployment baseline changes so an
# older disposable database cannot be mistaken for the current schema. Alembic
# stores it in a varchar(32), so it has to fit.
revision = "20260804_drop_boot_keys"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    DatabaseBase.metadata.create_all(connection)

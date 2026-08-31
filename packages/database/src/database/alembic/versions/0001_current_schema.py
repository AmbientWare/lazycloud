from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from database.tables import DatabaseBase

# This identifier must change whenever the predeployment baseline changes so an
# older disposable database cannot be mistaken for the current schema. Alembic
# stores it in a varchar(32), so it has to fit.
revision = "20260831_wireguard_network"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    DatabaseBase.metadata.create_all(op.get_bind())

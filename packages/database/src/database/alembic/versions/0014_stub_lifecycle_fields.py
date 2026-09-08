"""Remove obsolete top-level stub lifecycle configuration."""

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

revision = "0014_stub_lifecycle_fields"
down_revision = "0013_artifact_storage"
branch_labels = None
depends_on = None

_payload = TypeAdapter(dict[str, JsonValue])


def upgrade() -> None:
    connection = op.get_bind()
    stubs = sa.table(
        "stubs",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
    )
    obsolete = {"on_start", "on_deploy", "on_deploy_stub_id"}
    for row in connection.execute(sa.select(stubs).with_for_update()).mappings():
        payload = _payload.validate_python(row["payload"])
        config = payload.get("config")
        if not isinstance(config, dict) or not obsolete.intersection(config):
            continue
        for key in obsolete:
            config.pop(key, None)
        connection.execute(stubs.update().where(stubs.c.id == row["id"]).values(payload=payload))


def downgrade() -> None:
    raise RuntimeError("obsolete stub lifecycle values cannot be reconstructed")

"""Remove obsolete CloudFormation console authorization URLs."""

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

revision = "0012_aws_stack_actions"
down_revision = "0011_supplier_cost_terms"
branch_labels = None
depends_on = None

_payload = TypeAdapter(dict[str, JsonValue])


def upgrade() -> None:
    connection = op.get_bind()
    records = sa.table(
        "aws_account_connections",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
    )
    for row in connection.execute(sa.select(records)).mappings():
        payload = _payload.validate_python(row["payload"])
        for key in ("active_authorization", "pending_authorization", "retiring_authorization"):
            authorization = payload.get(key)
            if not isinstance(authorization, dict):
                continue
            if authorization.get("authorization_url"):
                raise RuntimeError(
                    "Cancel or remove pending AWS connection authorizations through the "
                    "connection lifecycle before the object-storage cutover."
                )
            authorization.pop("authorization_url", None)
            authorization["authorization_stack"] = None
        connection.execute(
            records.update().where(records.c.id == row["id"]).values(payload=payload)
        )


def downgrade() -> None:
    raise RuntimeError("stack authorization cannot restore obsolete S3 console actions")

"""Remove lifecycle hooks the platform never invokes."""

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

revision = "0015_unimplemented_hooks"
down_revision = "0014_stub_lifecycle_fields"
branch_labels = None
depends_on = None

_payload = TypeAdapter(dict[str, JsonValue])
_obsolete = {"on_cancelled", "on_timeout"}


def upgrade() -> None:
    connection = op.get_bind()
    for table_name, parent_key in (("stubs", "config"), ("deployments", "spec")):
        records = sa.table(
            table_name,
            sa.column("id", sa.Uuid(as_uuid=False)),
            sa.column("payload", sa.JSON()),
        )
        for row in connection.execute(sa.select(records).with_for_update()).mappings():
            payload = _payload.validate_python(row["payload"])
            parent = payload.get(parent_key)
            if not isinstance(parent, dict) or not _remove_hooks(parent.get("lifecycle_hooks")):
                continue
            connection.execute(
                records.update().where(records.c.id == row["id"]).values(payload=payload)
            )

    containers = sa.table(
        "containers",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("workspace_id", sa.Uuid(as_uuid=False)),
        sa.column("stub_id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
    )
    stubs = sa.table(
        "stubs",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("workspace_id", sa.Uuid(as_uuid=False)),
        sa.column("type", sa.String()),
    )
    statement = (
        sa.select(containers)
        .join(
            stubs,
            (containers.c.stub_id == stubs.c.id)
            & (containers.c.workspace_id == stubs.c.workspace_id),
        )
        .where(stubs.c.type.in_(("function", "endpoint", "asgi")))
        .with_for_update(of=containers)
    )
    for row in connection.execute(statement).mappings():
        payload = _payload.validate_python(row["payload"])
        env = payload.get("env")
        if not isinstance(env, dict):
            continue
        raw = env.get("LIFECYCLE_HOOKS")
        if not isinstance(raw, str) or not raw:
            continue
        hooks = _payload.validate_json(raw)
        if not _remove_hooks(hooks):
            continue
        env["LIFECYCLE_HOOKS"] = _payload.dump_json(hooks).decode()
        connection.execute(
            containers.update().where(containers.c.id == row["id"]).values(payload=payload)
        )


def _remove_hooks(hooks: JsonValue) -> bool:
    if not isinstance(hooks, dict) or not _obsolete.intersection(hooks):
        return False
    for key in _obsolete:
        hooks.pop(key, None)
    return True


def downgrade() -> None:
    raise RuntimeError("removed lifecycle hook values cannot be reconstructed")

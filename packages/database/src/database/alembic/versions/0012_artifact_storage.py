"""Index task artifacts and start prospective storage metering."""

from datetime import UTC, datetime
from urllib.parse import unquote

import sqlalchemy as sa
from alembic import op
from pydantic import JsonValue, TypeAdapter

revision = "0012_artifact_storage"
down_revision = "0011_supplier_cost_terms"
branch_labels = None
depends_on = None

_payload = TypeAdapter(dict[str, JsonValue])


def upgrade() -> None:
    for name in ("artifact_task_id", "artifact_app_id"):
        op.add_column("objects", sa.Column(name, sa.String(64), nullable=True))
    for name in ("artifact_expires_at", "artifact_metered_at"):
        op.add_column("objects", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_objects_artifact_listing",
        "objects",
        ["workspace_id", "artifact_task_id", "created_at", "id"],
    )
    op.create_index("ix_objects_artifact_app", "objects", ["workspace_id", "artifact_app_id"])
    op.create_index("ix_objects_artifact_expiration", "objects", ["artifact_expires_at", "id"])
    op.create_index("ix_objects_artifact_metering", "objects", ["artifact_metered_at", "id"])
    op.create_table(
        "artifact_retention",
        sa.Column(
            "workspace_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("retention_seconds", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("retention_seconds > 0", name="ck_artifact_retention_positive"),
    )
    connection = op.get_bind()
    activation = datetime.now(UTC)
    objects = sa.table(
        "objects",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("key", sa.String()),
        sa.column("workspace_id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
        sa.column("artifact_task_id", sa.String()),
        sa.column("artifact_app_id", sa.String()),
        sa.column("artifact_metered_at", sa.DateTime(timezone=True)),
    )
    tasks = sa.table(
        "tasks",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("workspace_id", sa.Uuid(as_uuid=False)),
        sa.column("payload", sa.JSON()),
    )
    apps = sa.table(
        "apps",
        sa.column("id", sa.Uuid(as_uuid=False)),
        sa.column("workspace_id", sa.Uuid(as_uuid=False)),
        sa.column("name", sa.String()),
    )
    for row in connection.execute(
        sa.select(objects).where(objects.c.key.startswith("artifacts/"))
    ).mappings():
        payload = _payload.validate_python(row["payload"])
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("artifact_id") != str(row["id"]):
            continue
        if not isinstance(metadata.get("task_id"), str):
            raise RuntimeError(f"artifact object lacks task identity: {row['id']}")
        filename = metadata.get("filename")
        fields: dict[str, JsonValue] = {
            "artifact_task_id": metadata["task_id"],
            "artifact_filename": unquote(filename)
            if isinstance(filename, str)
            else str(row["key"]).rsplit("/", 1)[-1],
            "artifact_retention_source": "workspace",
            "artifact_retention_seconds": None,
            "artifact_metered_at": activation.isoformat(),
        }
        task_payload = connection.scalar(
            sa.select(tasks.c.payload).where(
                tasks.c.id == metadata["task_id"], tasks.c.workspace_id == row["workspace_id"]
            )
        )
        if task_payload is not None:
            task = _payload.validate_python(task_payload)
            app_id = task.get("app_id")
            if isinstance(app_id, str) and app_id:
                fields["artifact_app_id"] = app_id
                fields["artifact_app_name"] = (
                    connection.scalar(
                        sa.select(apps.c.name).where(
                            apps.c.id == app_id, apps.c.workspace_id == row["workspace_id"]
                        )
                    )
                    or ""
                )
        pending = payload.get("write_claimed_at") is not None
        if not pending:
            fields.update(
                artifact_stored_at=payload.get("created_at"),
                artifact_metered_at=activation.isoformat(),
            )
        payload.update(fields)
        target = payload.get("write_target")
        if isinstance(target, dict):
            target.update(fields)
        connection.execute(
            objects.update()
            .where(objects.c.id == row["id"])
            .values(
                payload=payload,
                artifact_task_id=metadata["task_id"],
                artifact_app_id=fields.get("artifact_app_id"),
                artifact_metered_at=activation,
            )
        )


def downgrade() -> None:
    raise RuntimeError("artifact billing and retention records cannot be discarded")

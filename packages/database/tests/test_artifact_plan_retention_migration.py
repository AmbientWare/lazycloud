from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from alembic import command
from database.migrations import alembic_config, bootstrap_database
from database.tables.billing import BillingAccountTable
from database.tables.identity import UserTable, WorkspaceMemberTable, WorkspaceTable
from database.tables.storage import ObjectTable
from shared.objects import ObjectRecord, ObjectWriteCommand
from shared.timestamps import utc_now
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import URL


def test_rollout_bounds_legacy_artifacts_without_shortening_grace_or_changing_other_objects(
    postgres_database_url: URL,
) -> None:
    url = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "0030_withdraw_rate_schedule")
    engine = create_engine(url)
    now = utc_now()
    expected: dict[str, tuple[int, datetime | None]] = {}
    try:
        with engine.begin() as connection:
            for plan, complimentary, days in (
                ("free", False, 1),
                ("team", False, 30),
                ("business", False, 90),
                ("free", True, 30),
            ):
                user_id, workspace_id = str(uuid4()), str(uuid4())
                connection.execute(insert(UserTable).values(id=user_id))
                connection.execute(
                    insert(WorkspaceTable).values(id=workspace_id, name=workspace_id, payload={})
                )
                connection.execute(
                    insert(WorkspaceMemberTable).values(
                        workspace_id=workspace_id, user_id=user_id, role="owner", payload={}
                    )
                )
                connection.execute(
                    insert(BillingAccountTable).values(
                        user_id=user_id,
                        status="active",
                        plan=plan,
                        complimentary_since=now if complimentary else None,
                    )
                )
                cases: list[tuple[str, datetime | None]] = [("indefinite", None)]
                if plan == "free" and not complimentary:
                    cases.extend(
                        [
                            ("earlier", now + timedelta(hours=1)),
                            ("longer", now + timedelta(days=365)),
                            ("pending", None),
                        ]
                    )
                for name, expiry in cases:
                    record = ObjectRecord(
                        id=str(uuid4()),
                        bucket="objects",
                        key=name,
                        path=f"s3://objects/{name}",
                        size=6,
                        sha256="a" * 64,
                        artifact_task_id=str(uuid4()),
                        artifact_retention_seconds=1,
                        artifact_filename=name,
                        artifact_expires_at=expiry,
                        created_at=now - timedelta(days=365),
                        metadata={"retention_source": "customer metadata"},
                    )
                    payload = record.model_dump(mode="json")
                    payload["artifact_retention_source"] = "workspace"
                    payload["artifact_retention_seconds"] = None
                    if name == "pending":
                        target = ObjectWriteCommand(
                            bucket=record.bucket,
                            key=record.key,
                            path=record.path,
                            size=record.size,
                            sha256=record.sha256,
                        ).model_dump(mode="json")
                        target["artifact_task_id"] = record.artifact_task_id
                        target["artifact_retention_source"] = "workspace"
                        payload["write_target"] = target
                        payload["write_claimed_at"] = now.isoformat()
                    connection.execute(
                        insert(ObjectTable).values(
                            id=record.id,
                            workspace_id=workspace_id,
                            bucket=record.bucket,
                            key=name,
                            size=record.size,
                            sha256=record.sha256,
                            content_type="text/plain",
                            artifact_task_id=record.artifact_task_id,
                            artifact_expires_at=expiry,
                            write_claimed_at=now if name == "pending" else None,
                            payload=payload,
                        )
                    )
                    expected[record.id] = (days, expiry if name == "earlier" else None)
            ordinary = ObjectRecord(
                id=str(uuid4()),
                bucket="objects",
                key="customer-file",
                path="s3://objects/file",
                size=1,
                sha256="b" * 64,
                metadata={"retention_source": "customer metadata"},
            )
            ordinary_payload = ordinary.model_dump(mode="json")
            connection.execute(
                insert(ObjectTable).values(
                    id=ordinary.id,
                    workspace_id=workspace_id,
                    bucket=ordinary.bucket,
                    key=ordinary.key,
                    size=ordinary.size,
                    sha256=ordinary.sha256,
                    content_type="text/plain",
                    payload=ordinary_payload,
                )
            )
        before = utc_now()
        bootstrap_database(url)
        after = utc_now()
        with engine.connect() as connection:
            rows = connection.execute(select(ObjectTable)).mappings().all()
            assert {row["id"] for row in rows} == {*expected, ordinary.id}
            for row in rows:
                if row["id"] == ordinary.id:
                    assert row["payload"] == ordinary_payload
                    assert row["artifact_expires_at"] is None
                    continue
                record = ObjectRecord.model_validate(row["payload"])
                days, earlier = expected[record.id]
                assert record.artifact_expires_at == row["artifact_expires_at"]
                assert record.artifact_expires_at is not None
                if earlier is not None:
                    assert record.artifact_expires_at == earlier
                else:
                    assert before + timedelta(days=days) <= record.artifact_expires_at
                    assert record.artifact_expires_at <= after + timedelta(days=days)
                assert record.metadata == {"retention_source": "customer metadata"}
                assert record.size == 6
                if record.write_target is not None:
                    assert record.write_target.artifact_retention_seconds == days * 86400
    finally:
        engine.dispose()

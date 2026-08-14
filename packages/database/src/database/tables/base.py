from __future__ import annotations

from datetime import UTC, datetime

from pydantic import JsonValue
from sqlalchemy import DDL, DateTime, ForeignKey, String, Uuid, event, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def utc_now() -> datetime:
    return datetime.now(UTC)


class DatabaseBase(DeclarativeBase):
    pass


json_type = JSON().with_variant(JSONB(), "postgresql")
uuid_type = Uuid(as_uuid=False)


def _require_extension(name: str) -> None:
    event.listen(
        DatabaseBase.metadata,
        "before_create",
        DDL(f"CREATE EXTENSION IF NOT EXISTS {name}").execute_if(dialect="postgresql"),
    )


# Declared with the metadata rather than in the migration because the schema
# cannot be created without them at all: every path that builds it needs them,
# not only the one that goes through Alembic. `pgcrypto` backs the
# `gen_random_uuid()` server defaults below; `btree_gist` is what lets an
# exclusion constraint index an equality column beside a range.
_require_extension("pgcrypto")
_require_extension("btree_gist")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class PayloadMixin:
    payload: Mapped[dict[str, JsonValue]] = mapped_column(json_type, nullable=False)


class IdTable(TimestampMixin):
    __abstract__ = True

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )


class IdPayloadTable(IdTable, PayloadMixin):
    __abstract__ = True


class NamedWorkspacePayloadTable(TimestampMixin, PayloadMixin):
    __abstract__ = True

    id: Mapped[str] = mapped_column(
        uuid_type,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        uuid_type,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )

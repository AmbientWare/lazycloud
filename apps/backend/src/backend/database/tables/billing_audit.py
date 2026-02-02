import uuid

from sqlalchemy import UUID, Index, String
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.tables.base import BaseTable


class BillingAuditLogTable(BaseTable):
    """Immutable audit trail for billing operations."""

    __tablename__ = "billing_audit_log"

    event_type: Mapped[str] = mapped_column(String, index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    actor: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_billing_audit_workspace_date", "workspace_id", "created_at"),
    )

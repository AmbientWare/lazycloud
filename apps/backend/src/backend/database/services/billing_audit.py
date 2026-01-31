from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import BillingAuditLogPydantic
from backend.database.tables import BillingAuditLogTable


class BillingEventType(StrEnum):
    COLLECTION_STARTED = "collection_started"
    COLLECTION_COMPLETED = "collection_completed"
    COLLECTION_FAILED = "collection_failed"
    COLLECTION_SKIPPED = "collection_skipped"
    BILLING_STARTED = "billing_started"
    BILLING_COMPLETED = "billing_completed"
    BILLING_FAILED = "billing_failed"
    BILLING_SKIPPED = "billing_skipped"
    BILLING_RETRY = "billing_retry"


class BillingAuditService:
    """Service for creating billing audit log entries."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def log_event(
        self,
        event_type: BillingEventType,
        workspace_id: str,
        record_id: str | None = None,
        actor: str | None = None,
        details: dict | None = None,
    ) -> BillingAuditLogPydantic:
        """Create an audit log entry."""
        audit_entry = BillingAuditLogTable(
            event_type=event_type.value,
            workspace_id=workspace_id,
            record_id=record_id,
            actor=actor or "system",
            details=details or {},
        )
        self._session.add(audit_entry)
        await self._session.flush()
        await self._session.refresh(audit_entry)
        return audit_entry.to_pydantic(BillingAuditLogPydantic)

    async def log_collection_completed(
        self,
        workspace_id: str,
        record_id: str,
        interval_start: datetime,
        cpu_seconds: float,
        memory_seconds: float,
    ) -> BillingAuditLogPydantic:
        """Log successful interval collection."""
        return await self.log_event(
            event_type=BillingEventType.COLLECTION_COMPLETED,
            workspace_id=workspace_id,
            record_id=record_id,
            details={
                "interval_start": interval_start.isoformat(),
                "cpu_core_seconds": cpu_seconds,
                "memory_gb_seconds": memory_seconds,
            },
        )

    async def log_billing_started(
        self,
        workspace_id: str,
        record_id: str,
        usage_date: str,
        attempt: int,
    ) -> BillingAuditLogPydantic:
        """Log billing attempt started."""
        return await self.log_event(
            event_type=BillingEventType.BILLING_STARTED,
            workspace_id=workspace_id,
            record_id=record_id,
            details={
                "usage_date": usage_date,
                "attempt": attempt,
            },
        )

    async def log_billing_completed(
        self,
        workspace_id: str,
        record_id: str,
        billing_id: str,
        usage_date: str,
    ) -> BillingAuditLogPydantic:
        """Log successful billing."""
        return await self.log_event(
            event_type=BillingEventType.BILLING_COMPLETED,
            workspace_id=workspace_id,
            record_id=record_id,
            details={
                "usage_date": usage_date,
                "billing_id": billing_id,
            },
        )

    async def log_billing_failed(
        self,
        workspace_id: str,
        record_id: str,
        error: str,
        attempt: int,
        usage_date: str,
    ) -> BillingAuditLogPydantic:
        """Log failed billing attempt."""
        return await self.log_event(
            event_type=BillingEventType.BILLING_FAILED,
            workspace_id=workspace_id,
            record_id=record_id,
            details={
                "usage_date": usage_date,
                "error": error[:500],
                "attempt": attempt,
            },
        )

    async def log_billing_skipped(
        self,
        workspace_id: str,
        record_id: str,
        reason: str,
        usage_date: str,
    ) -> BillingAuditLogPydantic:
        """Log skipped billing (e.g., incomplete intervals or max attempts exceeded)."""
        return await self.log_event(
            event_type=BillingEventType.BILLING_SKIPPED,
            workspace_id=workspace_id,
            record_id=record_id,
            details={
                "usage_date": usage_date,
                "reason": reason,
            },
        )

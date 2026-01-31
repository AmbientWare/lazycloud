from backend.database.models.base import BaseDbModel, UUIDStr


class BillingAuditLog(BaseDbModel):
    event_type: str
    workspace_id: UUIDStr
    record_id: UUIDStr | None = None
    actor: str | None = None
    details: dict = {}

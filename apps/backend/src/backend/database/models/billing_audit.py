from backend.database.models.base import BaseDbPydanticModel, UUIDStr


class BillingAuditLogPydantic(BaseDbPydanticModel):
    event_type: str
    workspace_id: UUIDStr
    record_id: UUIDStr | None = None
    actor: str | None = None
    details: dict = {}

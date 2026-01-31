from pydantic import BaseModel

from backend.database.models.base import BaseDbModel, UUIDStr


class BillingAuditLog(BaseModel):
    event_type: str
    workspace_id: UUIDStr
    record_id: UUIDStr | None = None
    actor: str | None = None
    details: dict = {}


class BillingAuditLogInDb(BillingAuditLog, BaseDbModel):
    """Pydantic model for a billing audit log that is stored in the database"""

    ...

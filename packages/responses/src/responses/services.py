from datetime import UTC, datetime

from models.statuses import ServiceStatus
from pydantic import BaseModel


class ServiceStatusResponse(BaseModel):
    """Response for service status endpoint."""

    deployment_id: str
    deployment_name: str
    namespace: str
    service: ServiceStatus
    last_checked: datetime = datetime.now(UTC)

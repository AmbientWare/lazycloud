from datetime import UTC, datetime

from pydantic import BaseModel

from shared.models.statuses import ServiceStatus


class ServiceStatusResponse(BaseModel):
    """Response for service status endpoint."""

    deployment_id: str
    deployment_name: str
    namespace: str
    service: ServiceStatus
    last_checked: datetime = datetime.now(UTC)

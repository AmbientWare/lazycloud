"""Pydantic models for service status responses."""

from datetime import datetime
from typing import List

from pydantic import BaseModel

from shared.models.statuses import PodStatus, ServiceStatus


class ServiceStatusResponse(BaseModel):
    """Response for service status endpoint."""

    deployment_id: str
    deployment_name: str
    namespace: str
    service: ServiceStatus
    pods: List[PodStatus]
    last_updated: datetime

from __future__ import annotations

from pydantic import AwareDatetime, model_validator

from shared.http.base import HttpModel
from shared.network_egress import NetworkEgressRouteEvidence
from shared.usage import UsageBillingOwner


class WorkerEgressPolicyRequest(HttpModel):
    pass


class WorkerEgressPolicy(HttpModel):
    billing_owner: UsageBillingOwner
    routes: NetworkEgressRouteEvidence | None = None
    verified_at: AwareDatetime

    @model_validator(mode="after")
    def require_platform_routes(self) -> WorkerEgressPolicy:
        if self.billing_owner is UsageBillingOwner.PlatformFleet and self.routes is None:
            raise ValueError("platform internet egress requires verified provider routes")
        return self

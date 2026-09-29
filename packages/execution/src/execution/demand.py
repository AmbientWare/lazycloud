from __future__ import annotations

import logging
from dataclasses import dataclass

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS
from coordination.wake_signal import WakeSignalPublisher
from database.repositories.orchestration import AutoscalingTargetRepository
from database.types import DatabaseSession
from shared.autoscaler_state import AutoscalerTargetKind

PLACEMENT_WAKE_SCOPE = "scheduler-placement"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionDemandService:
    wake: WakeSignalPublisher

    def activate_in_transaction(
        self,
        session: DatabaseSession,
        *,
        stub_id: str,
        workspace_id: str,
        kind: AutoscalerTargetKind,
    ) -> None:
        AutoscalingTargetRepository(session).activate(
            stub_id=stub_id,
            workspace_id=workspace_id,
            target_kind=kind,
        )

    def notify(self) -> None:
        try:
            self.wake.signal()
        except REDIS_UNAVAILABLE_ERRORS:
            LOGGER.warning("placement wake unavailable; durable demand remains due", exc_info=True)

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from time import monotonic
from typing import Protocol

from shared.container_requests import StopContainerReason
from shared.funding import FUNDING_RENEWAL_SECONDS, FundingPermit
from shared.http.worker_funding import WorkerFundingRequest
from shared.timestamps import utc_now

from worker.execution import OciLinuxResources

LOGGER = logging.getLogger(__name__)


class WorkerFundingClient(Protocol):
    def authorize_container_funding(self, request: WorkerFundingRequest) -> FundingPermit: ...

    def renew_container_funding(self, request: WorkerFundingRequest) -> FundingPermit: ...


class FundedContainerStopper(Protocol):
    def prepare_funded_runtime(self, container_id: str, resources: OciLinuxResources) -> None: ...

    def require_funded_stop(self, container_id: str) -> None: ...

    def stop_container(
        self, container_id: str, *, force: bool, reason: StopContainerReason
    ) -> None: ...


@dataclass(slots=True)
class WorkerFundingSession:
    client: WorkerFundingClient
    permit: FundingPermit
    stop: Callable[[], None]
    _closed: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _deadline: float = field(default=0, init=False)
    _expired: bool = field(default=False, init=False)

    def start(self) -> None:
        self._deadline = monotonic() + (self.permit.valid_until - utc_now()).total_seconds()
        self.require_valid()
        threading.Thread(
            target=self._watch_deadline,
            name=f"funding-deadline-{self.permit.container_id}",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._renew, name=f"funding-renewal-{self.permit.container_id}", daemon=True
        ).start()

    def require_valid(self) -> None:
        with self._lock:
            if self._expired or self._closed.is_set() or monotonic() >= self._deadline:
                raise RuntimeError("the container's funded runtime permit expired")

    def close(self) -> None:
        self._closed.set()

    def _watch_deadline(self) -> None:
        while not self._closed.wait(0.1):
            with self._lock:
                if monotonic() < self._deadline:
                    continue
                self._expired = True
            try:
                self.stop()
            except Exception:
                LOGGER.exception(
                    "funded runtime stop failed", extra={"container_id": self.permit.container_id}
                )
                continue
            return

    def _renew(self) -> None:
        while not self._closed.wait(FUNDING_RENEWAL_SECONDS):
            try:
                permit = self.client.renew_container_funding(
                    WorkerFundingRequest(container_id=self.permit.container_id)
                )
                deadline = monotonic() + (permit.valid_until - utc_now()).total_seconds()
                with self._lock:
                    if self._expired or self._closed.is_set() or monotonic() >= self._deadline:
                        return
                    if (
                        permit.container_id != self.permit.container_id
                        or permit.revision <= self.permit.revision
                    ):
                        raise RuntimeError("funding renewal did not advance the container permit")
                    self.permit = permit
                    self._deadline = deadline
            except Exception:
                LOGGER.warning(
                    "funded runtime renewal failed",
                    exc_info=True,
                    extra={"container_id": self.permit.container_id},
                )


@dataclass(frozen=True, slots=True)
class WorkerFundingSupervisor:
    client: WorkerFundingClient

    def begin(self, container_id: str, *, stop: Callable[[], None]) -> WorkerFundingSession:
        permit = self.client.authorize_container_funding(
            WorkerFundingRequest(container_id=container_id)
        )
        if permit.container_id != container_id:
            raise RuntimeError("funding authorization returned another container's permit")
        session = WorkerFundingSession(client=self.client, permit=permit, stop=stop)
        session.start()
        return session

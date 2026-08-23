from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from shared.contracts import ContractModel
from shared.http_transport import HttpChannel
from shared.signals import (
    DEFAULT_SIGNAL_SET_TTL_SECONDS,
    SignalClearRequest,
    SignalClearResponse,
    SignalMonitorRequest,
    SignalMonitorResponse,
    SignalMonitorSnapshot,
    SignalSetRequest,
    SignalSetResponse,
    SignalStatus,
    normalize_clear_after_interval,
    signal_name,
)

from lazycloud.control import workspace_query
from lazycloud.transport_retry import call_with_transient_retry


class SignalControlChannel(Protocol):
    def get(self, path: str) -> Any: ...

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...


class SignalSetBody(ContractModel):
    ttl_seconds: int = DEFAULT_SIGNAL_SET_TTL_SECONDS


@dataclass
class SignalControlClient:
    channel: SignalControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> SignalControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def set(
        self,
        name: str,
        *,
        ttl_seconds: int = DEFAULT_SIGNAL_SET_TTL_SECONDS,
    ) -> SignalSetResponse:
        return self._set(name, ttl_seconds=ttl_seconds, workspace=self.workspace)

    def clear(self, name: str) -> SignalClearResponse:
        return self._clear(name, workspace=self.workspace)

    def monitor_once(self, name: str) -> SignalMonitorResponse:
        return self._monitor_response(name, workspace=self.workspace)

    def monitor(
        self,
        name: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_events: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> Iterator[SignalMonitorResponse]:
        emitted = 0
        while max_events is None or emitted < max_events:
            yield call_with_transient_retry(lambda: self.monitor_once(name), sleep=sleeper)
            emitted += 1
            if max_events is None or emitted < max_events:
                sleeper(poll_interval_seconds)

    def signal_set(self, request: SignalSetRequest) -> SignalSetResponse:
        return self._set(
            request.name,
            ttl_seconds=request.ttl_seconds,
            workspace=request.workspace_name,
        )

    def signal_clear(self, request: SignalClearRequest) -> SignalClearResponse:
        return self._clear(request.name, workspace=request.workspace_name)

    def signal_monitor_once(
        self,
        request: SignalMonitorRequest,
        *,
        has_handler: bool = False,
        clear_after_interval_seconds: int | None = None,
    ) -> SignalMonitorSnapshot:
        response = self._monitor_response(request.name, workspace=request.workspace_name)
        clear_after_seconds = normalize_clear_after_interval(clear_after_interval_seconds)
        should_call_handler = response.set and has_handler
        return SignalMonitorSnapshot(
            status=_monitor_status(response),
            signal_key=signal_name(request.workspace_name, request.name),
            ok=True,
            set=response.set,
            should_call_handler=should_call_handler,
            should_clear_after_handler=should_call_handler and clear_after_seconds > 0,
            clear_after_seconds=clear_after_seconds,
            response=response,
        )

    def _set(
        self,
        name: str,
        *,
        ttl_seconds: int,
        workspace: str,
    ) -> SignalSetResponse:
        request = SignalSetBody(ttl_seconds=ttl_seconds)
        return SignalSetResponse.model_validate(
            self.channel.post(
                self._path(name, "set", workspace=workspace),
                request.model_dump(mode="json"),
            )
        )

    def _clear(self, name: str, *, workspace: str) -> SignalClearResponse:
        return SignalClearResponse.model_validate(
            self.channel.post(self._path(name, "clear", workspace=workspace))
        )

    def _monitor_response(self, name: str, *, workspace: str) -> SignalMonitorResponse:
        return SignalMonitorResponse.model_validate(
            self.channel.get(self._path(name, "monitor", workspace=workspace))
        )

    def _path(self, name: str, suffix: str, *, workspace: str) -> str:
        query = urlencode(workspace_query(workspace))
        return f"/api/v1/signals/{quote(name, safe='')}/{suffix}?{query}"


def _monitor_status(response: SignalMonitorResponse) -> SignalStatus:
    if response.set:
        return SignalStatus.Found
    return SignalStatus.Missing


__all__ = [
    "SignalControlChannel",
    "SignalControlClient",
    "SignalSetBody",
]

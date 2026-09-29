from __future__ import annotations

import http.client
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from networking.async_http import (
    AsyncBackendHttpClient,
    AsyncBackendHttpError,
    AsyncBackendTimeoutError,
)
from networking.dialer import (
    BackendRouteDialer,
)
from networking.sync_http import BackendHttpConnectionPool
from pydantic import JsonValue, TypeAdapter
from shared.contracts import ContractModel
from shared.errors import UpstreamTimeoutError, UpstreamUnavailableError
from shared.http.pods import PodSandboxResultResponse
from shared.http.workspace_sync import WORKSPACE_SYNC_CONTENT_TYPE, WorkspaceSyncBatch
from shared.scheduling import SchedulerContainerAddress
from worker.container_client.control import ContainerServiceTransport
from worker.container_client.models import (
    ContainerClientConnectionOptions,
    ContainerSandboxResultRequest,
    ContainerSandboxResultResponse,
    ContainerServiceMethod,
    ContainerServiceWireValue,
)
from worker.container_client.wire import (
    CONTAINER_SERVICE_HTTP_PREFIX as _CONTAINER_SERVICE_HTTP_PREFIX,
)
from worker.container_client.wire import (
    decode_container_service_wire_value as _decode_container_service_wire_value,
)
from worker.container_client.wire import (
    encode_container_service_wire_value as _encode_container_service_wire_value,
)

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(slots=True)
class AsyncSandboxResultHttpClient:
    http: AsyncBackendHttpClient
    token: str = field(repr=False)

    async def sandbox_result(
        self,
        address: SchedulerContainerAddress,
        container_id: str,
        process_id: str,
        wait_seconds: float,
    ) -> PodSandboxResultResponse:
        if address.route is None:
            raise UpstreamUnavailableError("Container control requires an authorized backend route")
        request = ContainerSandboxResultRequest(
            container_id=container_id, process_id=process_id, wait_seconds=wait_seconds
        )
        try:
            response = await self.http.open_stream(
                address=address.address,
                route_id=address.route.route_id,
                method="POST",
                path=f"{_CONTAINER_SERVICE_HTTP_PREFIX}/{ContainerServiceMethod.ContainerSandboxResult.value}",
                headers={
                    "content-type": "application/json",
                    "authorization": f"Bearer {self.token}",
                },
                body=request.model_dump_json().encode(),
                timeout_seconds=wait_seconds + 3,
                resource=f"container {container_id} process result",
            )
            try:
                body = await response.read()
                if response.status_code >= 400:
                    raise UpstreamUnavailableError(
                        f"Container result returned HTTP {response.status_code}"
                    )
            finally:
                await response.close()
        except AsyncBackendTimeoutError as exc:
            raise UpstreamTimeoutError("Container service exceeded its request deadline") from exc
        except AsyncBackendHttpError as exc:
            raise UpstreamUnavailableError("Container service connection failed") from exc
        result = ContainerSandboxResultResponse.model_validate_json(body)
        if not result.ok:
            raise UpstreamUnavailableError(result.error_msg or "sandbox result unavailable")
        if result.result is None or result.result.process_id != process_id:
            raise UpstreamUnavailableError("sandbox supervisor returned no matching process result")
        return result.result


@dataclass(slots=True)
class HttpContainerServiceTransportFactory:
    route_dialer: BackendRouteDialer
    connections: BackendHttpConnectionPool = field(init=False)

    def __post_init__(self) -> None:
        self.connections = BackendHttpConnectionPool(self.route_dialer)

    def close(self) -> None:
        self.connections.close()

    def create_transport(
        self,
        options: ContainerClientConnectionOptions,
    ) -> ContainerServiceTransport:
        return HttpContainerServiceTransport(
            options,
            connections=self.connections,
        )


@dataclass(slots=True)
class HttpContainerServiceTransport:
    options: ContainerClientConnectionOptions
    connections: BackendHttpConnectionPool

    def unary(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> ContainerServiceWireValue:
        body = self._post(method, request, timeout_seconds=timeout_seconds, stream=False)
        if not body:
            return {}
        return _decode_container_service_wire_value(_JSON_VALUE.validate_json(body))

    def stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterable[ContainerServiceWireValue]:
        return self._stream(method, request, timeout_seconds=timeout_seconds)

    def _post(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None,
        stream: bool,
    ) -> bytes:
        path = f"{_CONTAINER_SERVICE_HTTP_PREFIX}/{method.value}"
        if stream:
            path = f"{path}/stream"
        if isinstance(request, WorkspaceSyncBatch):
            payload = request.encode()
            content_length = request.encoded_size
        else:
            payload = json.dumps(
                _encode_container_service_wire_value(request.model_dump(mode="python")),
                separators=(",", ":"),
            ).encode("utf-8")
            content_length = len(payload)
        headers = {
            "accept": "application/json",
            "content-type": WORKSPACE_SYNC_CONTENT_TYPE
            if isinstance(request, WorkspaceSyncBatch)
            else "application/json",
            "content-length": str(content_length),
            **self.options.auth_metadata,
        }
        with self._connection(timeout_seconds) as connection:
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                message = f"container service {method.value} returned HTTP {response.status}"
                raise UpstreamUnavailableError(message)
            return body

    def _stream(
        self,
        method: ContainerServiceMethod,
        request: ContractModel,
        *,
        timeout_seconds: float | None,
    ) -> Iterable[ContainerServiceWireValue]:
        path = f"{_CONTAINER_SERVICE_HTTP_PREFIX}/{method.value}/stream"
        payload = json.dumps(
            _encode_container_service_wire_value(request.model_dump(mode="python")),
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {
            "accept": "application/x-ndjson",
            "content-type": "application/json",
            "content-length": str(len(payload)),
            **self.options.auth_metadata,
        }
        with self._connection(timeout_seconds) as connection:
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            if response.status >= 400:
                message = f"container service {method.value} returned HTTP {response.status}"
                raise UpstreamUnavailableError(message)
            while True:
                line = response.readline()
                if not line:
                    break
                clean = line.strip()
                if clean:
                    yield _decode_container_service_wire_value(_JSON_VALUE.validate_json(clean))

    @contextmanager
    def _connection(self, timeout_seconds: float | None) -> Iterator[http.client.HTTPConnection]:
        timeout = (
            self.connections.route_dialer.config.timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        if not self.options.backend_route_id:
            raise UpstreamUnavailableError("Container control requires an authorized backend route")
        try:
            with self.connections.connection(
                self.options.backend_route_id,
                timeout,
            ) as connection:
                yield connection
        except TimeoutError as exc:
            raise UpstreamTimeoutError("Container service exceeded its request deadline") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamUnavailableError("Container service connection failed") from exc


__all__ = [
    "AsyncSandboxResultHttpClient",
    "HttpContainerServiceTransport",
    "HttpContainerServiceTransportFactory",
]

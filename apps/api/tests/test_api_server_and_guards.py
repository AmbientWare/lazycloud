from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from api.control_runtime import ControlPlaneRuntime, ControlPlaneRuntimeState
from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.agent_control import TailnetPeerView
from coordination.redis_client import RedisClient, RedisWireScalar
from fastapi.testclient import TestClient
from networking.tailnet import TailnetRuntimeError
from shared.http.system import HealthResponse
from tests.redis_fakes import FakeRedis


class _FailingRedis(FakeRedis):
    def ping(self, **_kwargs: RedisWireScalar) -> bool:
        return False


class _FailingTailnetRuntime:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True
        raise TailnetRuntimeError("tailnet sidecar is not authenticated (NeedsLogin)")

    def close(self) -> None:
        self.closed = True

    def wait_for_peer(self, host: str, timeout_seconds: float) -> None:
        _ = host, timeout_seconds

    def resolve_peer_host(self, host: str) -> str:
        return host

    def peers(self) -> list[TailnetPeerView]:
        return []


class _FatalTailnetRuntime(_FailingTailnetRuntime):
    def start(self) -> None:
        self.started = True
        raise RuntimeError("tailnet startup failed")


class _RecordingTelemetry:
    def __init__(self, *, fail_shutdown: bool = False) -> None:
        self.closed = False
        self.fail_shutdown = fail_shutdown

    def shutdown(self) -> None:
        self.closed = True
        if self.fail_shutdown:
            raise RuntimeError("telemetry close failed")


class _RecordingTcpIngress:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.started = False
        self.closed = False
        self.fail_start = fail_start
        self.fail_close = fail_close

    async def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("TCP ingress start failed")

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("TCP ingress close failed")


def test_control_plane_runtime_start_is_one_shot_under_concurrency(
    isolated_services: ApiServices,
) -> None:
    runtime = ControlPlaneRuntime.from_services(isolated_services)
    barrier = Barrier(2)

    def start_runtime() -> ApiServices | RuntimeError:
        barrier.wait()
        try:
            return runtime.start()
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(start_runtime) for _ in range(2)]
        results = [future.result() for future in futures]

    assert sum(isinstance(result, ApiServices) for result in results) == 1
    failures = [result for result in results if isinstance(result, RuntimeError)]
    assert len(failures) == 1
    assert "cannot start from serving" in str(failures[0])
    runtime.stop()
    runtime.stop()
    assert runtime.state is ControlPlaneRuntimeState.Closed


def test_control_plane_runtime_factory_failure_is_terminal() -> None:
    def fail_factory() -> ApiServices:
        raise RuntimeError("service graph failed")

    runtime = ControlPlaneRuntime(_factory=fail_factory)

    with pytest.raises(RuntimeError, match="service graph failed"):
        runtime.start()

    assert runtime.state is ControlPlaneRuntimeState.Closed
    with pytest.raises(RuntimeError, match="cannot start from closed"):
        runtime.start()
    runtime.stop()


def test_control_plane_health_endpoint(isolated_services: ApiServices) -> None:
    with TestClient(create_app(isolated_services)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    health = HealthResponse.model_validate_json(response.content)
    assert health.ok is True
    assert health.status == "ok"
    assert health.checks["database"].ok is True
    assert health.checks["redis"].ok is True


def test_control_plane_health_endpoint_reports_dependency_failure(
    isolated_services: ApiServices,
) -> None:
    redis = RedisClient(_FailingRedis(), key_prefix="test")
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        redis_client=redis,
        binary_redis_client=redis,
    )
    with TestClient(create_app(services)) as client:
        response = client.get("/health")

    assert response.status_code == 503
    health = HealthResponse.model_validate_json(response.content)
    assert health.ok is False
    assert health.checks["redis"].error == "unhealthy"


def test_control_plane_starts_when_tailnet_sidecar_is_not_ready(
    isolated_services: ApiServices,
) -> None:
    tailnet_runtime = _FailingTailnetRuntime()
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        tailnet_runtime=tailnet_runtime,
    )

    with TestClient(create_app(services)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert HealthResponse.model_validate_json(response.content).ok is True
    assert tailnet_runtime.started is True
    assert tailnet_runtime.closed is True

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
from storage.workspace_storage_issuers import StoredWorkspaceStorageIssuer
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

    def self_dns_name(self) -> str:
        return ""

    def advertise_service(self, service: str, ports: tuple[int, ...]) -> str:
        _ = service, ports
        return ""


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


def test_liveness_answers_without_the_service_graph(
    isolated_services: ApiServices,
) -> None:
    """Liveness must not depend on anything that can be broken or slow.

    The probe decides whether to kill the process, and both times production
    crashlooped it was a pod that could not reach a dependency being restarted
    into a start that needed that dependency. A `/livez` that resolved services
    or opened a connection would restore exactly that.
    """

    redis = RedisClient(_FailingRedis(), key_prefix="test")
    services = ApiServices.create(
        isolated_services.database,
        workspace_storage_issuer=StoredWorkspaceStorageIssuer(),
        root=isolated_services.root,
        create_schema=False,
        redis_client=redis,
        binary_redis_client=redis,
    )
    app = create_app(services)
    # Read before any request, because a dependency resolved at import time
    # would answer here and fail in a pod that has not published its services.
    assert getattr(app.state, "api_services", None) is None
    with TestClient(app) as client:
        assert client.get("/health").status_code == 503
        assert client.get("/livez").status_code == 204


def test_control_plane_health_endpoint_reports_dependency_failure(
    isolated_services: ApiServices,
) -> None:
    redis = RedisClient(_FailingRedis(), key_prefix="test")
    services = ApiServices.create(
        isolated_services.database,
        workspace_storage_issuer=StoredWorkspaceStorageIssuer(),
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


def test_control_plane_refuses_to_start_without_its_tailnet(
    isolated_services: ApiServices,
) -> None:
    """A control plane reaches every agent over the tailnet, so one that cannot
    join serves nothing. It used to log a warning and come up reporting healthy,
    which is how a broken tailnet stayed invisible for hours."""
    tailnet_runtime = _FailingTailnetRuntime()
    services = ApiServices.create(
        isolated_services.database,
        workspace_storage_issuer=StoredWorkspaceStorageIssuer(),
        root=isolated_services.root,
        create_schema=False,
        redis_client=isolated_services.redis_client,
        binary_redis_client=isolated_services.binary_redis_client,
        tailnet_runtime=tailnet_runtime,
    )

    with pytest.raises(BaseException) as error, TestClient(create_app(services)):
        pass

    assert tailnet_runtime.started is True
    assert "NeedsLogin" in str(error.value) or isinstance(error.value, BaseExceptionGroup)

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from api.control_runtime import ControlPlaneRuntime, ControlPlaneRuntimeState
from api.fastapi_app import create_app
from api.server.services import ApiServices
from coordination.redis_client import RedisClient, RedisWireScalar
from fastapi.testclient import TestClient
from shared.http.system import HealthResponse
from tests.redis_fakes import FakeRedis


class _FailingRedis(FakeRedis):
    def ping(self, **_kwargs: RedisWireScalar) -> bool:
        return False


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


def test_control_plane_health_endpoint(api_runtime: tuple[ApiServices, TestClient]) -> None:
    _, client = api_runtime
    response = client.get("/health")
    assert response.status_code == 200
    health = HealthResponse.model_validate_json(response.content)
    assert health.ok is True
    assert health.status == "ok"
    assert health.checks["database"].ok is True
    assert health.checks["redis"].ok is True


def test_dependency_failure_degrades_readiness_but_preserves_liveness(
    isolated_services: ApiServices,
) -> None:
    redis = RedisClient(_FailingRedis(), key_prefix="test")
    services = ApiServices.create(
        isolated_services.database,
        root=isolated_services.root,
        create_schema=False,
        redis_client=redis,
        binary_redis_client=redis,
        async_io=isolated_services.require_async_io(),
    )
    app = create_app(services)
    with TestClient(app) as client:
        response = client.get("/health")
        assert client.get("/livez").status_code == 204

    assert response.status_code == 503
    health = HealthResponse.model_validate_json(response.content)
    assert health.ok is False
    assert health.checks["redis"].error == "unhealthy"

from __future__ import annotations

from typing import Literal

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from coordination.redis_client import RedisClient
from execution.endpoints.service import EndpointControlService
from execution.functions.service import FunctionControlService
from execution.taskqueues.service import TaskQueueControlService
from shared.errors import ConflictError
from shared.function_payloads import FunctionJsonInvocation
from shared.http.endpoints import StartEndpointServeRequest
from shared.http.functions import FunctionInvokeBody
from shared.http.taskqueues import StartTaskQueueServeRequest
from shared.tasks import TaskStatus
from tests.redis_fakes import FakeRedis


@pytest.mark.parametrize("producer", ["function", "endpoint", "taskqueue"])
def test_paused_app_rejects_every_execution_producer_without_container_orphans(
    isolated_services: ApiServices,
    producer: Literal["function", "endpoint", "taskqueue"],
) -> None:
    app = isolated_services.apps.create(f"paused_{producer}")
    kind = {
        "function": StubKind.Function,
        "endpoint": StubKind.Endpoint,
        "taskqueue": StubKind.TaskQueue,
    }[producer]
    stub = ControlPlaneService(isolated_services.context).create_stub(
        producer,
        kind=kind,
        handler="pkg.workloads:handler",
        app_id=app.id,
        config={"image": {"image_id": f"image-{producer}"}},
    )
    isolated_services.apps.pause(app.id, workspace=app.workspace_id)

    if producer == "function":
        response = FunctionControlService(isolated_services).function_invoke(
            FunctionInvokeBody(
                stub_id=stub.id,
                invocation=FunctionJsonInvocation(args=[1]),
            )
        )
        assert response.task_id
        task = isolated_services.tasks.get(response.task_id)
        assert task.status is TaskStatus.Cancelled
        assert task.error == "owning app is not active"
    elif producer == "endpoint":
        with pytest.raises(ConflictError, match="is not active"):
            EndpointControlService(isolated_services).start_endpoint_serve(
                StartEndpointServeRequest(stub_id=stub.id)
            )
    else:
        with pytest.raises(ConflictError, match="is not active"):
            TaskQueueControlService(
                isolated_services,
                redis=RedisClient(FakeRedis(), key_prefix="paused-taskqueue"),
            ).start_task_queue_serve(StartTaskQueueServeRequest(stub_id=stub.id))

    assert isolated_services.containers.list(app_id=app.id) == []

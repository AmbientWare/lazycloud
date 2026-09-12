from __future__ import annotations

from typing import Literal

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from execution.endpoints.service import EndpointControlService
from execution.functions.service import FunctionControlService
from shared.errors import ConflictError
from shared.function_payloads import FunctionJsonInvocation
from shared.http.endpoints import EndpointWarmupRequest
from shared.http.functions import FunctionInvokeBody
from shared.tasks import TaskStatus


@pytest.mark.parametrize("producer", ["function", "endpoint"])
def test_paused_app_rejects_every_execution_producer_without_container_orphans(
    isolated_services: ApiServices,
    producer: Literal["function", "endpoint"],
) -> None:
    app = isolated_services.apps.create(f"paused_{producer}")
    kind = StubKind.Function if producer == "function" else StubKind.Endpoint
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
    else:
        with pytest.raises(ConflictError, match="is not active"):
            EndpointControlService(isolated_services).warm_endpoint(
                EndpointWarmupRequest(stub_id=stub.id)
            )

    assert isolated_services.containers.list(app_id=app.id) == []

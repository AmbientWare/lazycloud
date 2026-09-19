from __future__ import annotations

import pytest
from api.server.services import ApiServices
from control.service import ControlPlaneService, StubKind
from database.tables.execution import TaskTable
from database.tables.orchestration import ContainerTable
from execution.functions.service import FunctionControlService
from shared.errors import InvalidInputError
from shared.function_payloads import FunctionJsonInvocation
from shared.http.functions import FunctionInvokeBody
from sqlalchemy import func, select


def test_invoking_against_a_machine_that_is_gone_refuses_and_queues_nothing(
    isolated_services: ApiServices,
) -> None:
    """A stub names a machine; the placement is resolved when a run is requested.

    The stub is created without any machine joined, which is the same state a
    reused stub is in after its machine leaves. The refusal has to land before
    the task row, or the run would sit pending for capacity that never arrives.
    """
    stub = ControlPlaneService(isolated_services.context).create_stub(
        "on-gone-machine",
        kind=StubKind.Function,
        handler="pkg.workloads:handler",
        config={"image": {"image_id": "image-gone-machine"}, "machine": "rack-9"},
    )
    before = _row_counts(isolated_services, stub.workspace_id)
    with pytest.raises(InvalidInputError, match="'rack-9'"):
        FunctionControlService(isolated_services).function_invoke(
            FunctionInvokeBody(stub_id=stub.id, invocation=FunctionJsonInvocation(args=[1]))
        )
    assert _row_counts(isolated_services, stub.workspace_id) == before


def _row_counts(services: ApiServices, workspace_id: str) -> tuple[int, int]:
    with services.context.database.session() as session:
        tasks = session.scalar(select(func.count()).where(TaskTable.workspace_id == workspace_id))
        containers = session.scalar(
            select(func.count()).where(ContainerTable.workspace_id == workspace_id)
        )
    return int(tasks or 0), int(containers or 0)

from __future__ import annotations

from collections.abc import Iterable

from control.service import ControlPlaneService, StubKind, StubRecord
from fastapi import HTTPException, status
from shared.errors import NotFoundError
from shared.tasks import Task

from api.server.services import ApiServices

FUNCTION_STUB_KINDS = frozenset({StubKind.Function, StubKind.CronJob})
ENDPOINT_STUB_KINDS = frozenset({StubKind.Endpoint, StubKind.Asgi})
POD_STUB_KINDS = frozenset({StubKind.Pod, StubKind.Sandbox})


def require_stub_workspace(
    control_plane: ControlPlaneService,
    stub_id: str,
    workspace_id: str,
    *,
    resource_name: str,
    expected_kinds: StubKind | Iterable[StubKind] | None = None,
) -> StubRecord:
    try:
        stub = control_plane.get_stub(stub_id, workspace=workspace_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{resource_name} not found") from exc

    kinds = _expected_kind_set(expected_kinds)
    if kinds is not None and stub.kind not in kinds:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{resource_name} not found")
    return stub


def require_function_stub_workspace(
    control_plane: ControlPlaneService,
    stub_id: str,
    workspace_id: str,
) -> StubRecord:
    return require_stub_workspace(
        control_plane,
        stub_id,
        workspace_id,
        resource_name="function",
        expected_kinds=FUNCTION_STUB_KINDS,
    )


def require_task_queue_stub_workspace(
    control_plane: ControlPlaneService,
    stub_id: str,
    workspace_id: str,
) -> StubRecord:
    return require_stub_workspace(
        control_plane,
        stub_id,
        workspace_id,
        resource_name="task queue",
        expected_kinds=StubKind.TaskQueue,
    )


def require_endpoint_stub_workspace(
    control_plane: ControlPlaneService,
    stub_id: str,
    workspace_id: str,
) -> StubRecord:
    return require_stub_workspace(
        control_plane,
        stub_id,
        workspace_id,
        resource_name="endpoint",
        expected_kinds=ENDPOINT_STUB_KINDS,
    )


def require_pod_stub_workspace(
    control_plane: ControlPlaneService,
    stub_id: str,
    workspace_id: str,
) -> StubRecord:
    return require_stub_workspace(
        control_plane,
        stub_id,
        workspace_id,
        resource_name="pod",
        expected_kinds=POD_STUB_KINDS,
    )


def require_task_workspace(services: ApiServices, task_id: str, workspace_id: str) -> Task:
    try:
        task = services.tasks.get(task_id)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from exc

    if task.workspace_id != workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    return task


def _expected_kind_set(
    expected_kinds: StubKind | Iterable[StubKind] | None,
) -> frozenset[StubKind] | None:
    if expected_kinds is None:
        return None
    if isinstance(expected_kinds, StubKind):
        return frozenset({expected_kinds})
    return frozenset(expected_kinds)

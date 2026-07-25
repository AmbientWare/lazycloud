from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from control.service import ControlPlaneService
from database.repositories.orchestration import ContainerRepository
from operations.management import ManagementService
from shared.containers import ContainerRecord, ContainerStatus


def test_management_captures_only_active_workspace_shutdown_targets(
    isolated_services: ApiServices,
) -> None:
    control = ControlPlaneService(isolated_services.context)
    workspace = control.upsert_workspace("management-shutdown-targets")
    sibling = control.upsert_workspace("management-shutdown-sibling")
    active_ids: set[str] = set()

    with isolated_services.context.database.session() as session:
        repository = ContainerRepository(session)
        for name, status in (
            ("pending", ContainerStatus.Pending),
            ("running", ContainerStatus.Running),
            ("exited", ContainerStatus.Exited),
            ("failed", ContainerStatus.Failed),
            ("stopped", ContainerStatus.Stopped),
        ):
            container_id = str(uuid4())
            if status in {ContainerStatus.Pending, ContainerStatus.Running}:
                active_ids.add(container_id)
            repository.upsert(
                ContainerRecord(
                    id=container_id,
                    name=name,
                    image="",
                    command=[],
                    workspace_id=workspace.id,
                    runtime_worker_id=f"{name}-worker",
                    status=status,
                )
            )
        repository.upsert(
            ContainerRecord(
                id=str(uuid4()),
                name="sibling-running",
                image="",
                command=[],
                workspace_id=sibling.id,
                runtime_worker_id="sibling-worker",
                status=ContainerStatus.Running,
            )
        )

    targets = ManagementService(isolated_services).capture_active_container_shutdown_targets(
        workspace.id
    )

    assert {target.container_id for target in targets} == active_ids
    assert {target.worker_id for target in targets} == {"pending-worker", "running-worker"}

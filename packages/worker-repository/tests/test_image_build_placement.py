from uuid import uuid4

import pytest
from api.server.services import ApiServices
from database.repositories.orchestration import MachineRepository
from database.workspace_secrets import WorkspaceSecretCipher
from images.building import build_image_plan, plan_image_build_session
from images.execution import ImageBuildExecutionRequest
from shared.compute_fleet import Machine
from shared.errors import InvalidInputError
from shared.image_building.authoring import ImageSpec
from shared.placement import Placement
from tests.workspaces import owned_workspace
from worker_repository.image_build_dispatch import (
    DurableImageBuildDispatch,
    ImageBuildDispatchPayload,
)


def test_a_build_for_a_machine_pinned_workload_is_placed_on_that_machine(
    isolated_services: ApiServices,
) -> None:
    control = isolated_services.control_plane_service
    workspace = owned_workspace(control, "default")
    other = owned_workspace(control, "other-tenant")
    machine_id = str(uuid4())
    with isolated_services.database.session() as session:
        machines = MachineRepository(session)
        machines.upsert(
            Machine(
                id=machine_id,
                name="compose-agent",
                workspace_ids=(workspace.id,),
                placement=Placement.machine(machine_id),
            ),
            workspace_id=workspace.id,
        )
        foreign_id = str(uuid4())
        machines.upsert(
            Machine(
                id=foreign_id,
                name="foreign-agent",
                workspace_ids=(other.id,),
                placement=Placement.machine(foreign_id),
            ),
            workspace_id=other.id,
        )
    executor = isolated_services.images.submission.executor
    assert isinstance(executor, DurableImageBuildDispatch)

    def placement(machine: str) -> Placement:
        plan = build_image_plan(ImageSpec())
        build_id = str(uuid4())
        encrypted = executor.prepare(
            ImageBuildExecutionRequest(
                build_id=build_id,
                workspace_id=workspace.id,
                image_id=plan.image_id,
                build_dir=build_id,
                dockerfile_path=f"{build_id}/Dockerfile",
                manifest_path=f"{build_id}/manifest.json",
                plan=plan,
                session=plan_image_build_session(
                    plan.spec,
                    container_id=str(uuid4()),
                    image_id=plan.image_id,
                    build_id=build_id,
                ),
                machine=machine,
            )
        )
        payload = ImageBuildDispatchPayload.model_validate_json(
            WorkspaceSecretCipher.from_workspace(workspace).decrypt(
                f"image-build-dispatch:{build_id}", encrypted
            )
        )
        return payload.plan.scheduler_request.placement

    assert placement("compose-agent") == Placement.machine(machine_id)
    assert placement("") == Placement.platform()
    with pytest.raises(InvalidInputError, match="does not serve"):
        placement("foreign-agent")

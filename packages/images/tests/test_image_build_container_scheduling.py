from __future__ import annotations

import json
from pathlib import Path

from images.building import (
    build_image_plan,
    marshal_registry_credentials,
    plan_image_build_registry_credentials,
    plan_image_build_session,
    registry_credentials_for_image,
)
from images.building.models import ImageBuildCredentialAction
from images.execution import ImageBuildExecutionRequest
from images.scheduling import (
    IMAGE_BUILD_REQUEST_KIND,
    ImageBuildSchedulerCredentialSource,
    image_build_scheduling_failure,
    plan_image_build_container_request,
)
from pydantic import JsonValue
from scheduler.fleet import SchedulerWorkerStatus
from scheduler.state import SchedulerWorkerRecord, SchedulerWorkerRequest
from shared.compute_policy import MachinePool
from shared.image_building.authoring import ImageSpec
from shared.image_building.credentials import ImageCredentialEnvVar
from shared.scheduling import SchedulerContainerState, SchedulerContainerStatus


def test_image_build_scheduling_failure_requires_failed_image_build_state() -> None:
    failure = image_build_scheduling_failure(
        SchedulerContainerState(
            container_id="build-container-1",
            stub_id=IMAGE_BUILD_REQUEST_KIND,
            workspace_id="workspace-1",
            status=SchedulerContainerStatus.Failed,
            image_build_id="build-1",
            failure_reason="no worker capacity available",
        )
    )

    assert failure is not None
    assert failure.container_id == "build-container-1"
    assert failure.build_id == "build-1"
    assert failure.workspace_id == "workspace-1"
    assert failure.reason == "no worker capacity available"
    assert (
        image_build_scheduling_failure(
            SchedulerContainerState(
                container_id="container-1",
                stub_id="function",
                workspace_id="workspace-1",
                status=SchedulerContainerStatus.Failed,
                failure_reason="no worker capacity available",
            )
        )
        is None
    )


def test_unmodified_private_image_uses_ephemeral_credentials_during_build(
    tmp_path: Path,
) -> None:
    credentials = {
        ImageCredentialEnvVar.RegistryUsername.value: "builder-user",
        ImageCredentialEnvVar.RegistryPassword.value: "secret-password",
    }
    source_image = "registry.example.com/team/worker:latest"
    request = _request(
        tmp_path,
        ImageSpec(base=source_image, ignore_python=True),
    )
    credential_plan = plan_image_build_registry_credentials(
        source_image=source_image,
        credentials=credentials,
    )
    registry_payload = marshal_registry_credentials(
        registry_credentials_for_image(source_image, credentials)
    )
    request = request.model_copy(
        update={
            "credential_plan": credential_plan,
            "registry_credential_payload": registry_payload,
        }
    )

    plan = plan_image_build_container_request(request, workspace_id="workspace-1")
    concurrent = plan_image_build_container_request(request, workspace_id="workspace-1")
    metadata = plan.credential_metadata
    payload_json = json.dumps(plan.scheduler_request.payload, sort_keys=True)

    assert metadata.action is ImageBuildCredentialAction.UseSourcePullCredentials
    assert metadata.source is ImageBuildSchedulerCredentialSource.EphemeralPrivateInputs
    assert metadata.cache_key.startswith(
        f"build-credential:workspace-1:{request.build_id}:{request.session.container_id}:"
    )
    assert concurrent.credential_metadata.cache_key != metadata.cache_key
    assert "secret-password" not in payload_json
    assert "builder-user" not in payload_json


def _request(
    tmp_path: Path,
    image: ImageSpec,
    *,
    build_args: dict[str, str] | None = None,
) -> ImageBuildExecutionRequest:
    plan = build_image_plan(image)
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    dockerfile_path = build_dir / "Dockerfile"
    manifest_path = build_dir / "manifest.json"
    dockerfile_path.write_text(plan.dockerfile, encoding="utf-8")
    manifest_path.write_text("{}", encoding="utf-8")
    session = plan_image_build_session(
        image,
        image_id=plan.image_id,
        build_id="build-1",
        container_id="build-container-1",
    )
    return ImageBuildExecutionRequest(
        build_id="build-1",
        image_id=plan.image_id,
        tag="local:test",
        build_dir=str(build_dir),
        dockerfile_path=str(dockerfile_path),
        manifest_path=str(manifest_path),
        plan=plan,
        session=session,
        build_args=build_args or {},
    )


def _json_object(value: JsonValue, *, name: str) -> dict[str, JsonValue]:
    assert isinstance(value, dict), f"{name} must be a JSON object"
    return value


class _FakeWorkerRequestRepository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, SchedulerWorkerRequest]] = []

    def schedule_container_request(
        self,
        worker_id: str,
        request: SchedulerWorkerRequest,
    ) -> SchedulerWorkerRecord:
        self.calls.append((worker_id, request))
        if self.error is not None:
            raise self.error
        return SchedulerWorkerRecord(
            capacity_owner_id="11111111-1111-4111-8111-111111111111",
            worker_id=worker_id,
            pool=MachinePool("default"),
            status=SchedulerWorkerStatus.Available,
        )

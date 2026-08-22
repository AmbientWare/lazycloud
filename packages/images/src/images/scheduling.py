from __future__ import annotations

from enum import StrEnum
from uuid import uuid4

from pydantic import Field, JsonValue
from shared.contracts import ContractModel
from shared.image_building.authoring import LinuxArchitecture
from shared.managed_runtime_integrity import managed_package_source_digest
from shared.scheduling import (
    SchedulerContainerState,
    SchedulerContainerStatus,
    SchedulerWorkerRequest,
)

from images.building.models import (
    ImageBuildCredentialAction,
    ImageRegistryCredentialKind,
)
from images.building.references import image_build_source_plan
from images.execution import ImageBuildExecutionRequest

DEFAULT_IMAGE_BUILD_CONTAINER_CPU_MILLICORES = 1000
DEFAULT_IMAGE_BUILD_CONTAINER_MEMORY_MIB = 1024
DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_WAIT_SECONDS = 180.0
DEFAULT_IMAGE_BUILD_CONTAINER_ADDRESS_POLL_SECONDS = 0.1
DEFAULT_SCHEDULER_BUILD_REGISTRY_CREDENTIAL_TTL_SECONDS = 5 * 60
IMAGE_BUILD_REQUEST_KIND = "image-build"


class ImageBuildSchedulerCredentialSource(StrEnum):
    None_ = "none"
    EphemeralPrivateInputs = "ephemeral-private-inputs"


class ImageBuildContainerBuildOptions(ContractModel):
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    managed_package_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    source_image: str = ""
    dockerfile: str = ""
    build_context_object: str = ""
    build_context_path: str = ""
    build_context_digest: str = ""
    build_secret_names: list[str] = Field(default_factory=list)
    build_arg_names: list[str] = Field(default_factory=list)


class ImageBuildContainerCredentialMetadata(ContractModel):
    action: ImageBuildCredentialAction = ImageBuildCredentialAction.Skip
    registry: str = ""
    repository: str = ""
    kind: ImageRegistryCredentialKind = ImageRegistryCredentialKind.Public
    credential_keys: list[str] = Field(default_factory=list)
    source: ImageBuildSchedulerCredentialSource = ImageBuildSchedulerCredentialSource.None_
    cache_key: str = ""
    ttl_seconds: int = 0
    reason: str = ""


class ImageBuildContainerRequestPlan(ContractModel):
    scheduler_request: SchedulerWorkerRequest
    build_options: ImageBuildContainerBuildOptions
    credential_metadata: ImageBuildContainerCredentialMetadata
    reason: str = ""


class ImageBuildSchedulingFailureEvidence(ContractModel):
    container_id: str
    build_id: str
    workspace_id: str
    reason: str


def plan_image_build_container_request(
    request: ImageBuildExecutionRequest,
    *,
    workspace_id: str,
    stub_id: str = "",
    pool_selector: str = "",
    cpu_millicores: int = DEFAULT_IMAGE_BUILD_CONTAINER_CPU_MILLICORES,
    memory_mib: int = DEFAULT_IMAGE_BUILD_CONTAINER_MEMORY_MIB,
) -> ImageBuildContainerRequestPlan:
    build_options = ImageBuildContainerBuildOptions(
        architecture=request.plan.spec.architecture,
        managed_package_digest=managed_package_source_digest(),
        source_image=request.plan.spec.base,
        dockerfile=request.plan.dockerfile,
        build_context_object=request.plan.spec.context_object_id or "",
        build_context_path=(
            request.plan.spec.context_path if not request.plan.spec.context_object_id else ""
        )
        or "",
        build_context_digest=request.plan.context_digest or "",
        build_secret_names=sorted(set(request.plan.spec.secrets)),
        build_arg_names=sorted(
            name for name, value in request.build_args.items() if name and value
        ),
    )
    credential_metadata = plan_image_build_scheduler_credential_metadata(
        request,
        workspace_id=workspace_id,
    )
    payload: dict[str, JsonValue] = {
        "kind": IMAGE_BUILD_REQUEST_KIND,
        "build_id": request.build_id,
        "image_id": request.image_id,
        "tag": request.tag,
        "dockerfile_path": request.dockerfile_path,
        "manifest_path": request.manifest_path,
        "build_dir": request.build_dir,
        "build_options": build_options.model_dump(mode="json"),
        "credential_metadata": credential_metadata.model_dump(mode="json"),
        "archive_upload_capability": uuid4().hex,
        "session": {
            "clip_version": request.session.clip_version,
            "ttl_seconds": request.session.ttl_seconds,
            "keepalive_interval_seconds": request.session.keepalive_interval_seconds,
        },
        "env": [f"{key}={value}" for key, value in sorted(request.plan.spec.env.items())],
    }
    gpu = request.plan.spec.gpu or ""
    scheduler_request = SchedulerWorkerRequest(
        workspace_id=workspace_id,
        stub_id=stub_id or IMAGE_BUILD_REQUEST_KIND,
        container_id=request.session.container_id,
        cpu_millicores=cpu_millicores,
        memory_mib=memory_mib,
        gpu_type=gpu,
        gpu_request=[gpu] if gpu else [],
        gpu_count=1 if gpu else 0,
        pool_selector="" if gpu else pool_selector.strip(),
        # No explicit selector means the workspace policy decides where the build
        # runs. Forcing Managed strands every build on a deployment whose capacity
        # is a connected provider: the request queues for local capacity that does
        # not exist and retries until it gives up.
        architecture=request.plan.spec.architecture.value,
        payload=payload,
    )
    return ImageBuildContainerRequestPlan(
        scheduler_request=scheduler_request,
        build_options=build_options,
        credential_metadata=credential_metadata,
        reason="image build container worker request planned",
    )


def image_build_scheduling_failure(
    state: SchedulerContainerState | None,
) -> ImageBuildSchedulingFailureEvidence | None:
    if (
        state is None
        or state.status is not SchedulerContainerStatus.Failed
        or not state.image_build_id
    ):
        return None
    return ImageBuildSchedulingFailureEvidence(
        container_id=state.container_id,
        build_id=state.image_build_id,
        workspace_id=state.workspace_id,
        reason=state.failure_reason or "image build container scheduling failed",
    )


def plan_image_build_scheduler_credential_metadata(
    request: ImageBuildExecutionRequest,
    *,
    workspace_id: str = "",
) -> ImageBuildContainerCredentialMetadata:
    credential_plan = request.credential_plan
    if credential_plan is None:
        return ImageBuildContainerCredentialMetadata(reason="no credential plan")
    reference = image_build_source_plan(request.plan.spec).reference
    repository = reference.repository if reference is not None else ""
    metadata = ImageBuildContainerCredentialMetadata(
        action=credential_plan.action,
        registry=credential_plan.registry,
        repository=repository,
        kind=credential_plan.kind,
        credential_keys=sorted(credential_plan.credential_keys),
        reason=credential_plan.reason,
    )
    if request.registry_credential_payload or request.build_args:
        cache_key = _build_credential_capability(
            workspace_id=workspace_id or request.workspace_id,
            build_id=request.build_id,
            container_id=request.session.container_id,
        )
        return metadata.model_copy(
            update={
                "source": ImageBuildSchedulerCredentialSource.EphemeralPrivateInputs,
                "cache_key": cache_key,
                "ttl_seconds": DEFAULT_SCHEDULER_BUILD_REGISTRY_CREDENTIAL_TTL_SECONDS,
            }
        )
    if not credential_plan.use_source_pull_credentials:
        return metadata
    return metadata.model_copy(
        update={
            "source": ImageBuildSchedulerCredentialSource.EphemeralPrivateInputs,
            "ttl_seconds": DEFAULT_SCHEDULER_BUILD_REGISTRY_CREDENTIAL_TTL_SECONDS,
        }
    )


def _build_credential_capability(
    *,
    workspace_id: str,
    build_id: str,
    container_id: str,
) -> str:
    if not workspace_id or not build_id or not container_id:
        raise ValueError(
            "image build credential capability requires workspace, build, and container"
        )
    return f"build-credential:{workspace_id}:{build_id}:{container_id}:{uuid4().hex}"

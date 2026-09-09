from __future__ import annotations

from dataclasses import dataclass

from database.repositories.identity import WorkspaceRepository
from database.repositories.image_build_dispatch import ImageBuildDispatchRepository
from database.repositories.images import ImageBuildRepository
from execution.secrets.crypto import WorkspaceSecretCipher
from execution.services import ExecutionContainerService
from identity.auth import AuthorizationDeniedError
from images.building import registry_auth_file_entry, unmarshal_registry_credentials
from images.execution import ImageBuildExecutionRequest
from images.scheduling import ImageBuildContainerRequestPlan, plan_image_build_container_request
from images.settings import ImageBuildContainerSettings
from pydantic import Field
from scheduler.containers import SchedulerContainerRequestService
from shared.container_requests import StopContainerReason
from shared.contracts import ContractModel
from shared.errors import NotFoundError, UpstreamUnavailableError
from shared.identity import WorkspaceRecord, WorkspaceStatus
from shared.image_building.records import BuildStatus
from worker.repository_payloads import ImageBuildPrivateInputs, ImageBuildRegistryAuth

from database import DatabaseClient


class ImageBuildDispatchPayload(ContractModel):
    plan: ImageBuildContainerRequestPlan
    private_inputs: ImageBuildPrivateInputs = Field(repr=False)


@dataclass(slots=True)
class DurableImageBuildDispatch:
    database: DatabaseClient
    scheduler: SchedulerContainerRequestService
    containers: ExecutionContainerService
    settings: ImageBuildContainerSettings

    def abort(self, build_id: str, workspace_id: str) -> None:
        with self.database.session() as session:
            record = ImageBuildRepository(session).get(build_id, workspace_id=workspace_id)
        if record is None:
            return
        try:
            self.containers.stop(
                build_id,
                reason=StopContainerReason.User
                if record.status is BuildStatus.Cancelled
                else StopContainerReason.Scheduler,
            )
        except NotFoundError:
            self.scheduler.cancel(build_id)

    def prepare(self, request: ImageBuildExecutionRequest) -> str:
        registry_auth: ImageBuildRegistryAuth | None = None
        if request.registry_credential_payload:
            credentials = unmarshal_registry_credentials(request.registry_credential_payload)
            entry = registry_auth_file_entry(credentials)
            registry_auth = ImageBuildRegistryAuth(
                registry=credentials.registry,
                auth=entry.get("auth", ""),
                identity_token=entry.get("identitytoken", ""),
            )
        payload = ImageBuildDispatchPayload(
            plan=plan_image_build_container_request(
                request,
                workspace_id=request.workspace_id,
                pool_selector=self.settings.pool_selector,
                cpu_millicores=self.settings.cpu_millicores,
                memory_mib=self.settings.memory_mib,
            ),
            private_inputs=ImageBuildPrivateInputs(
                registry_auth=registry_auth, build_args=request.build_args
            ),
        )
        workspace = _workspace(self.database, request.workspace_id)
        return WorkspaceSecretCipher.from_workspace(workspace).encrypt(
            f"image-build-dispatch:{request.build_id}", payload.model_dump_json()
        )

    def dispatch(self, build_id: str, workspace_id: str, payload: str) -> None:
        with self.database.session() as session:
            record = ImageBuildRepository(session).get(build_id, workspace_id=workspace_id)
        workspace = _workspace(self.database, workspace_id)
        if record is None or record.status not in {BuildStatus.Pending, BuildStatus.Running}:
            return
        prepared = ImageBuildDispatchPayload.model_validate_json(
            WorkspaceSecretCipher.from_workspace(workspace).decrypt(
                f"image-build-dispatch:{build_id}", payload
            )
        )
        request = prepared.plan.scheduler_request
        if request.container_id != build_id or request.workspace_id != workspace_id:
            raise AuthorizationDeniedError("image build dispatch identity does not match")
        self.containers.reserve_image_build_container(
            container_id=build_id,
            workspace_id=workspace_id,
            image_id=record.image_id or "",
        )
        result = self.scheduler.submit(request)
        if not result.accepted:
            raise UpstreamUnavailableError("image build dispatch was not accepted by the scheduler")


def image_build_private_inputs(
    database: DatabaseClient,
    *,
    workspace_id: str,
    build_id: str,
    container_id: str,
    registry: str,
    cache_key: str,
) -> ImageBuildPrivateInputs | None:
    with database.session() as session:
        encrypted = ImageBuildDispatchRepository(session).payload(
            build_id, workspace_id=workspace_id
        )
        if encrypted is None:
            return None
    workspace = _workspace(database, workspace_id)
    payload = ImageBuildDispatchPayload.model_validate_json(
        WorkspaceSecretCipher.from_workspace(workspace).decrypt(
            f"image-build-dispatch:{build_id}", encrypted
        )
    )
    metadata = payload.plan.credential_metadata
    if (
        container_id != build_id
        or payload.plan.scheduler_request.container_id != container_id
        or metadata.cache_key != cache_key
        or metadata.registry != registry
    ):
        raise AuthorizationDeniedError("image build private input binding does not match")
    return payload.private_inputs


def _workspace(database: DatabaseClient, workspace_id: str) -> WorkspaceRecord:
    with database.session() as session:
        workspace = WorkspaceRepository(session).get(workspace_id)
    if workspace is None or workspace.status is not WorkspaceStatus.Active:
        raise NotFoundError("image build workspace is not active")
    return workspace

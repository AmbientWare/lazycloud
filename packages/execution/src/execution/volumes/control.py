from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from control.service import ControlPlaneService
from database.repositories.identity import WorkspaceRepository
from database.repositories.storage import VolumeRepository
from shared.errors import ConflictError, InvalidInputError, NotFoundError
from shared.http.volumes import (
    AbortMultipartUploadRequest,
    AbortMultipartUploadResponse,
    CompleteMultipartUploadRequest,
    CompleteMultipartUploadResponse,
    CopyPathResponse,
    CreateMultipartUploadRequest,
    CreateMultipartUploadResponse,
    CreatePresignedUrlRequest,
    CreatePresignedUrlResponse,
    DeletePathRequest,
    DeletePathResponse,
    DeleteVolumeRequest,
    DeleteVolumeResponse,
    FileUploadPart,
    GetFileServiceInfoResponse,
    GetOrCreateVolumeRequest,
    GetOrCreateVolumeResponse,
    ListPathRequest,
    ListPathResponse,
    ListVolumesResponse,
    MovePathRequest,
    MovePathResponse,
    PathInfo,
    PresignedUrlMethod,
    StatPathRequest,
    StatPathResponse,
    VolumeInstance,
)
from shared.identity import WorkspaceRecord, WorkspaceStatus
from shared.volumes import VolumeRecord
from storage.volume_filesystem import (
    VolumeFilesystem,
    VolumeFilesystemEntry,
    VolumeNamespace,
)
from storage.volume_metering import PersistentVolumeMeteringService

from execution.admission import PaymentAdmission
from execution.context import ExecutionContext
from execution.volumes.planning import (
    VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS,
    clamp_presigned_url_expires,
    parse_volume_input,
    plan_volume_file_service_info,
    plan_volume_multipart_upload,
)
from execution.volumes.records import VolumeService


class VolumeControlDependencies(Protocol):
    @property
    def context(self) -> ExecutionContext: ...

    @property
    def volumes(self) -> VolumeService: ...

    @property
    def volume_metering(self) -> PersistentVolumeMeteringService: ...

    @property
    def payment_admission(self) -> PaymentAdmission: ...


@dataclass(frozen=True, slots=True)
class ResolvedVolume:
    workspace: WorkspaceRecord
    record: VolumeRecord
    namespace: VolumeNamespace


class VolumeControlService:
    def __init__(
        self,
        services: VolumeControlDependencies,
        *,
        filesystem: VolumeFilesystem,
    ) -> None:
        self.services = services
        self.control_plane = ControlPlaneService(services.context)
        self.filesystem = filesystem
        self.volume_metering = services.volume_metering

    def get_or_create_volume(
        self,
        request: GetOrCreateVolumeRequest,
        *,
        workspace_id: str = "default",
    ) -> GetOrCreateVolumeResponse:
        workspace = self.control_plane.get_workspace(workspace_id)
        # Admitted only where a volume would be created. Resolving one that
        # already exists is how a container mounts it and how its owner reads
        # their own files back, and refusing that would be a data-loss incident
        # wearing a billing control's clothes.
        record = self.services.volumes.get_or_create(
            request.name,
            workspace=workspace.id,
            admit=self.services.payment_admission,
        )
        namespace = VolumeNamespace(workspace_id=workspace.id, volume_id=record.id)
        self.filesystem.ensure_volume(namespace)
        return GetOrCreateVolumeResponse(volume=self._volume_instance(record, workspace))

    def delete_volume(
        self,
        request: DeleteVolumeRequest,
        *,
        workspace_id: str = "default",
    ) -> DeleteVolumeResponse:
        resolved = self._resolve_volume(request.name, workspace_id=workspace_id)
        self.volume_metering.finalize_volume_deletion(
            request.name,
            workspace_id=resolved.workspace.id,
        )
        self.filesystem.delete_volume(resolved.namespace)
        self.services.volumes.delete(request.name, workspace=resolved.workspace.id)
        return DeleteVolumeResponse()

    def delete_volume_for_workspace_deletion(
        self,
        request: DeleteVolumeRequest,
        *,
        workspace_id: str,
    ) -> DeleteVolumeResponse:
        """Delete physical storage then its existing record for a Deleting workspace."""
        resolved = self._resolve_volume_for_workspace_deletion(
            request.name,
            workspace_id=workspace_id,
        )
        self.volume_metering.finalize_volume_deletion(
            request.name,
            workspace_id=resolved.workspace.id,
        )
        self.filesystem.delete_volume(resolved.namespace)
        self.services.volumes.delete_for_workspace_deletion(
            request.name,
            workspace_id=resolved.workspace.id,
        )
        return DeleteVolumeResponse()

    def list_volumes(self, *, workspace_id: str = "default") -> ListVolumesResponse:
        workspace = self.control_plane.get_workspace(workspace_id)
        return ListVolumesResponse(
            volumes=tuple(
                self._volume_instance(record, workspace)
                for record in self.services.volumes.list(workspace=workspace.id)
            ),
        )

    def list_volumes_for_workspace_deletion(
        self,
        workspace_id: str,
    ) -> ListVolumesResponse:
        with self.services.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            records = VolumeRepository(session).list(workspace_id=workspace_id)
        return ListVolumesResponse(
            volumes=tuple(self._volume_instance(record, workspace) for record in records)
        )

    def list_path(
        self,
        request: ListPathRequest,
        *,
        workspace_id: str = "default",
    ) -> ListPathResponse:
        resolved, relative_path = self._resolve_path(request.path, workspace_id=workspace_id)
        return ListPathResponse(
            path_infos=tuple(
                self._path_info(entry)
                for entry in self.filesystem.list_path(resolved.namespace, relative_path)
            )
        )

    def stat_path(
        self,
        request: StatPathRequest,
        *,
        workspace_id: str = "default",
    ) -> StatPathResponse:
        resolved, relative_path = self._resolve_path(request.path, workspace_id=workspace_id)
        return StatPathResponse(
            path_info=self._path_info(self.filesystem.stat_path(resolved.namespace, relative_path))
        )

    def delete_path(
        self,
        request: DeletePathRequest,
        *,
        workspace_id: str = "default",
    ) -> DeletePathResponse:
        resolved, relative_path = self._resolve_path(
            request.path,
            workspace_id=workspace_id,
            require_file=True,
        )
        return DeletePathResponse(
            deleted=self.filesystem.delete_path(resolved.namespace, relative_path)
        )

    def copy_path(
        self,
        path: str,
        content: bytes,
        *,
        workspace_id: str = "default",
    ) -> CopyPathResponse:
        return self.copy_path_stream(path, (content,), workspace_id=workspace_id)

    def copy_path_stream(
        self,
        path: str,
        chunks: Iterable[bytes],
        *,
        workspace_id: str = "default",
    ) -> CopyPathResponse:
        resolved, relative_path = self._resolve_path(
            path,
            workspace_id=workspace_id,
            require_file=True,
        )
        self.filesystem.write_path(resolved.namespace, relative_path, chunks)
        return CopyPathResponse()

    def move_path(
        self,
        request: MovePathRequest,
        *,
        workspace_id: str = "default",
    ) -> MovePathResponse:
        source, source_path = self._resolve_path(
            request.original_path,
            workspace_id=workspace_id,
            require_file=True,
        )
        destination, destination_path = self._resolve_path(
            request.new_path,
            workspace_id=workspace_id,
            require_file=True,
        )
        if source.record.id != destination.record.id:
            raise InvalidInputError("moving across different volumes is not supported")
        self.filesystem.move_path(source.namespace, source_path, destination_path)
        return MovePathResponse(new_path=request.new_path)

    def get_file_service_info(self) -> GetFileServiceInfoResponse:
        return GetFileServiceInfoResponse.from_plan(plan_volume_file_service_info(enabled=True))

    def create_presigned_url(
        self,
        request: CreatePresignedUrlRequest,
        *,
        workspace_id: str = "default",
    ) -> CreatePresignedUrlResponse:
        resolved = self._resolve_volume(request.volume_name, workspace_id=workspace_id)
        relative_path = self._validated_relative_path(request.volume_path, require_file=True)
        return CreatePresignedUrlResponse(
            url=self.filesystem.create_presigned_url(
                resolved.namespace,
                relative_path,
                method=request.method,
                expires_seconds=clamp_presigned_url_expires(request.expires),
                upload_id=request.params.upload_id,
                part_number=request.params.part_number,
                content_length=request.params.content_length,
                content_type=request.params.content_type,
            )
        )

    def create_multipart_upload(
        self,
        request: CreateMultipartUploadRequest,
        *,
        workspace_id: str = "default",
    ) -> CreateMultipartUploadResponse:
        resolved = self._resolve_volume(request.volume_name, workspace_id=workspace_id)
        relative_path = self._validated_relative_path(request.volume_path, require_file=True)
        validation = plan_volume_multipart_upload(
            upload_id="validation",
            volume_name=request.volume_name,
            volume_path=relative_path,
            file_size=request.file_size,
            chunk_size=request.chunk_size,
        )
        if not validation.ok:
            raise InvalidInputError(validation.error_message)
        upload_id = self.filesystem.create_multipart_upload(
            resolved.namespace,
            relative_path,
        )
        plan = validation.model_copy(update={"upload_id": upload_id})
        return CreateMultipartUploadResponse(
            upload_id=upload_id,
            file_upload_parts=tuple(
                FileUploadPart(
                    number=part.number,
                    start=part.start,
                    end=part.end,
                    url=self.filesystem.create_presigned_url(
                        resolved.namespace,
                        relative_path,
                        method=PresignedUrlMethod.UploadPart,
                        expires_seconds=VOLUME_PRESIGNED_URL_MAX_EXPIRES_SECONDS,
                        upload_id=upload_id,
                        part_number=part.number,
                        content_length=part.end - part.start,
                    ),
                )
                for part in plan.parts
            ),
        )

    def complete_multipart_upload(
        self,
        request: CompleteMultipartUploadRequest,
        *,
        workspace_id: str = "default",
    ) -> CompleteMultipartUploadResponse:
        resolved = self._resolve_volume(request.volume_name, workspace_id=workspace_id)
        relative_path = self._validated_relative_path(request.volume_path, require_file=True)
        self.filesystem.complete_multipart_upload(
            resolved.namespace,
            relative_path,
            upload_id=request.upload_id,
            completed_parts=tuple((part.number, part.etag) for part in request.completed_parts),
        )
        return CompleteMultipartUploadResponse()

    def abort_multipart_upload(
        self,
        request: AbortMultipartUploadRequest,
        *,
        workspace_id: str = "default",
    ) -> AbortMultipartUploadResponse:
        resolved = self._resolve_volume(request.volume_name, workspace_id=workspace_id)
        relative_path = self._validated_relative_path(request.volume_path, require_file=True)
        self.filesystem.abort_multipart_upload(
            resolved.namespace,
            relative_path,
            upload_id=request.upload_id,
        )
        return AbortMultipartUploadResponse()

    def _volume_instance(
        self,
        record: VolumeRecord,
        workspace: WorkspaceRecord,
    ) -> VolumeInstance:
        namespace = VolumeNamespace(workspace_id=workspace.id, volume_id=record.id)
        return VolumeInstance(
            id=record.id,
            name=record.name,
            size=self.filesystem.occupancy_bytes(namespace),
            created_at=record.created_at,
            updated_at=record.created_at,
            workspace_id=workspace.id,
            workspace_name=workspace.name,
        )

    def _resolve_volume(
        self,
        name: str,
        *,
        workspace_id: str,
    ) -> ResolvedVolume:
        workspace = self.control_plane.get_workspace(workspace_id)
        with self.services.context.database.session() as session:
            record = VolumeRepository(session).get(name, workspace_id=workspace.id)
        if record is None:
            raise NotFoundError("unable to find volume")
        return ResolvedVolume(
            workspace=workspace,
            record=record,
            namespace=VolumeNamespace(workspace_id=workspace.id, volume_id=record.id),
        )

    def _resolve_volume_for_workspace_deletion(
        self,
        name: str,
        *,
        workspace_id: str,
    ) -> ResolvedVolume:
        with self.services.context.database.session() as session:
            workspace = WorkspaceRepository(session).lock_for_deletion(workspace_id)
            if workspace.status is not WorkspaceStatus.Deleting:
                raise ConflictError(f"workspace cleanup requires deleting state: {workspace_id}")
            record = VolumeRepository(session).get(name, workspace_id=workspace_id)
        if record is None:
            raise NotFoundError("unable to find volume")
        return ResolvedVolume(
            workspace=workspace,
            record=record,
            namespace=VolumeNamespace(workspace_id=workspace.id, volume_id=record.id),
        )

    def _resolve_path(
        self,
        input_path: str,
        *,
        workspace_id: str,
        require_file: bool = False,
    ) -> tuple[ResolvedVolume, str]:
        parsed = parse_volume_input(input_path)
        if not parsed.volume_name:
            raise InvalidInputError("must provide volume name")
        return (
            self._resolve_volume(parsed.volume_name, workspace_id=workspace_id),
            self._validated_relative_path(parsed.volume_path, require_file=require_file),
        )

    @staticmethod
    def _validated_relative_path(path: str, *, require_file: bool = False) -> str:
        parsed = parse_volume_input(f"volume/{path}").volume_path
        if parsed.startswith("../") or parsed == "..":
            raise InvalidInputError("parent directory does not exist")
        if require_file and parsed in {"", "."}:
            raise InvalidInputError("must provide path")
        return parsed

    @staticmethod
    def _path_info(entry: VolumeFilesystemEntry) -> PathInfo:
        return PathInfo(
            path=entry.path,
            size=entry.size,
            mod_time=entry.modified_at,
            is_dir=entry.is_dir,
        )


__all__ = ["VolumeControlDependencies", "VolumeControlService"]

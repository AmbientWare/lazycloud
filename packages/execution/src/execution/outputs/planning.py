from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import Field
from shared.contracts import ContractModel
from shared.http.outputs import DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS

OUTPUT_ROUTE_PREFIX = "/output"
DEFAULT_OUTPUTS_PATH = "/data/outputs"
DEFAULT_OUTPUTS_PREFIX = "outputs"


class OutputStorageMode(StrEnum):
    LocalFilesystem = "local-filesystem"
    WorkspaceObjectStorage = "workspace-object-storage"


class OutputPublicUrlStatus(StrEnum):
    CachedProxyUrl = "cached-proxy-url"
    PresignedObjectUrl = "presigned-object-url"
    InvalidRequest = "invalid-request"


class OutputPathPlan(ContractModel):
    workspace_name: str
    stub_external_id: str
    task_external_id: str
    output_id: str
    filename: str
    root_path: str
    file_path: str
    storage_prefix: str
    storage_key: str


class OutputStatPlan(ContractModel):
    output_id: str
    task_id: str
    filename: str
    mode: str
    size: int = Field(ge=0)
    accessed_at: datetime | None = None
    modified_at: datetime | None = None


class OutputPublicUrlPlan(ContractModel):
    status: OutputPublicUrlStatus
    output_id: str
    cache_key: str
    target_path: str
    public_url: str
    expires_seconds: int = Field(ge=0)
    storage_mode: OutputStorageMode

    @property
    def ok(self) -> bool:
        return self.status in {
            OutputPublicUrlStatus.CachedProxyUrl,
            OutputPublicUrlStatus.PresignedObjectUrl,
        }


def output_public_url_key(output_id: str) -> str:
    return f"output:{output_id}"


def output_task_root_path(
    workspace_name: str,
    stub_external_id: str,
    task_external_id: str,
    *,
    outputs_path: str = DEFAULT_OUTPUTS_PATH,
) -> str:
    return _join(outputs_path, workspace_name, stub_external_id, task_external_id)


def output_storage_prefix(stub_external_id: str, task_external_id: str) -> str:
    return _join(DEFAULT_OUTPUTS_PREFIX, stub_external_id, task_external_id)


def plan_output_path(
    workspace_name: str,
    stub_external_id: str,
    task_external_id: str,
    output_id: str,
    filename: str,
    *,
    outputs_path: str = DEFAULT_OUTPUTS_PATH,
) -> OutputPathPlan:
    safe_name = _safe_filename(filename)
    root = output_task_root_path(
        workspace_name,
        stub_external_id,
        task_external_id,
        outputs_path=outputs_path,
    )
    storage_prefix = output_storage_prefix(stub_external_id, task_external_id)
    return OutputPathPlan(
        workspace_name=workspace_name,
        stub_external_id=stub_external_id,
        task_external_id=task_external_id,
        output_id=output_id,
        filename=safe_name,
        root_path=root,
        file_path=_join(root, output_id, safe_name),
        storage_prefix=storage_prefix,
        storage_key=_join(storage_prefix, output_id, safe_name),
    )


def plan_output_public_url(
    *,
    output_id: str,
    target_path: str,
    gateway_external_url: str,
    expires_seconds: int = DEFAULT_OUTPUT_PUBLIC_URL_EXPIRES_SECONDS,
    storage_mode: OutputStorageMode = OutputStorageMode.LocalFilesystem,
    presigned_url: str = "",
) -> OutputPublicUrlPlan:
    cache_key = output_public_url_key(output_id)
    if storage_mode is OutputStorageMode.WorkspaceObjectStorage:
        if not presigned_url:
            return OutputPublicUrlPlan(
                status=OutputPublicUrlStatus.InvalidRequest,
                output_id=output_id,
                cache_key=cache_key,
                target_path=target_path,
                public_url="",
                expires_seconds=expires_seconds,
                storage_mode=storage_mode,
            )
        return OutputPublicUrlPlan(
            status=OutputPublicUrlStatus.PresignedObjectUrl,
            output_id=output_id,
            cache_key=cache_key,
            target_path=target_path,
            public_url=presigned_url,
            expires_seconds=expires_seconds,
            storage_mode=storage_mode,
        )

    return OutputPublicUrlPlan(
        status=OutputPublicUrlStatus.CachedProxyUrl,
        output_id=output_id,
        cache_key=cache_key,
        target_path=target_path,
        public_url=f"{gateway_external_url.rstrip('/')}{OUTPUT_ROUTE_PREFIX}/id/{output_id}",
        expires_seconds=expires_seconds,
        storage_mode=storage_mode,
    )


def _safe_filename(filename: str) -> str:
    normalized = filename.replace("\\", "/").rstrip("/")
    name = PurePosixPath(normalized).name
    return name or "output"


def _join(*parts: str) -> str:
    cleaned = [part.strip("/") for part in parts if part.strip("/")]
    prefix = "/" if parts and parts[0].startswith("/") else ""
    return prefix + "/".join(cleaned)

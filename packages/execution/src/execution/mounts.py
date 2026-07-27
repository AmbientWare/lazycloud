from __future__ import annotations

import posixpath
from collections.abc import Iterable, Mapping

from database.repositories.storage import ObjectRepository
from pydantic import BaseModel, JsonValue, TypeAdapter
from shared.container_requests import (
    DEFAULT_OBJECTS_PATH,
    DEFAULT_VOLUMES_PATH,
    DEFAULT_OUTPUTS_PATH,
    WORKER_CONTAINER_VOLUME_PATH,
    WORKER_USER_CODE_VOLUME,
    WORKER_USER_OUTPUT_VOLUME,
    RequestMount,
    RequestMountPointConfig,
    RequestMountType,
)
from shared.mounts import MountAuthMode, validate_mount_auth
from storage.service import ObjectStorage

from execution.context import ExecutionContext
from execution.volumes.records import VolumeService

DEFAULT_EXTERNAL_VOLUMES_PATH = "/tmp/external-volumes"
SOURCE_WORKSPACE_ROOT = "/tmp/{container_id}/workspace"
JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

type VolumeMountInput = BaseModel | dict[str, JsonValue]


def source_code_mounts(
    *,
    context: ExecutionContext,
    object_storage: ObjectStorage,
    workspace_id: str,
    workspace_name: str,
    object_id: str,
) -> list[RequestMount]:
    if not object_id:
        return []
    with context.database.session() as session:
        record = next(
            (
                item
                for item in ObjectRepository(session).list(workspace_id=workspace_id)
                if item.id == object_id
            ),
            None,
        )
    if record is None:
        return []

    download_url = ""
    try:
        download_url = object_storage.generate_presigned_get_url_for_workspace(
            workspace_id=workspace_id,
            bucket=record.bucket,
            key=record.key,
            expires_seconds=900,
        )
    except Exception:
        download_url = ""

    return [
        RequestMount(
            local_path=f"{DEFAULT_OBJECTS_PATH}/{workspace_name}/{object_id}",
            mount_path=WORKER_USER_CODE_VOLUME,
            source_object_id=object_id,
            source_sha256=record.sha256,
            source_download_url=download_url,
        )
    ]


def container_resource_mounts(
    *,
    context: ExecutionContext,
    object_storage: ObjectStorage,
    workspace_id: str,
    workspace_name: str,
    object_id: str,
    stub_id: str,
    container_id: str,
    volumes: Iterable[VolumeMountInput] | None = None,
) -> list[RequestMount]:
    mounts = source_code_mounts(
        context=context,
        object_storage=object_storage,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        object_id=object_id,
    )
    if stub_id:
        mounts.append(
            RequestMount(
                local_path=posixpath.join(DEFAULT_OUTPUTS_PATH, workspace_name, stub_id),
                mount_path=WORKER_USER_OUTPUT_VOLUME,
            )
        )
    mounts.extend(
        configured_volume_mounts(
            context=context,
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            container_id=container_id,
            volumes=volumes or (),
        )
    )
    return mounts


def container_resource_mounts_require_workspace_storage(
    *,
    context: ExecutionContext,
    workspace_id: str,
    mounts: Iterable[RequestMount],
) -> bool:
    with context.database.session() as session:
        workspace = context.workspace(session, workspace_id)
    if not workspace.storage.bucket:
        return False
    return any(_mount_requires_workspace_storage(mount) for mount in mounts)


def configured_volume_mounts(
    *,
    context: ExecutionContext,
    workspace_id: str,
    workspace_name: str,
    container_id: str,
    volumes: Iterable[VolumeMountInput],
) -> list[RequestMount]:
    mounts: list[RequestMount] = []
    volume_service = VolumeService(context)
    for raw_volume in volumes:
        volume = _mapping(raw_volume)
        name = _raw_text(volume.get("id") or volume.get("name"))
        if not name:
            continue
        mount_path = _raw_text(volume.get("mount_path")) or name
        config = _mapping(volume.get("config"))
        read_only = _config_bool(config, "read_only")
        canonical_mount_path, root_mount_path = volume_container_mount_paths(
            mount_path,
            fallback_name=name,
        )
        record = volume_service.get(name, workspace=workspace_id)
        mount = _volume_request_mount(
            name=name,
            workspace_name=workspace_name,
            container_id=container_id,
            mount_path=canonical_mount_path,
            link_path=_volume_link_path(container_id, mount_path),
            read_only=read_only,
            config=config,
            local_path=platform_volume_local_path(
                workspace_name=workspace_name, volume_id=record.id
            ),
        )
        if root_mount_path and root_mount_path != canonical_mount_path:
            mounts.append(
                _volume_request_mount(
                    name=name,
                    workspace_name=workspace_name,
                    container_id=container_id,
                    mount_path=root_mount_path,
                    link_path="",
                    read_only=read_only,
                    config=config,
                    local_path=platform_volume_local_path(
                        workspace_name=workspace_name, volume_id=record.id
                    ),
                )
            )
        mounts.append(mount)
    return mounts


def _mount_requires_workspace_storage(mount: RequestMount) -> bool:
    if mount.mount_type in {RequestMountType.MountPoint, RequestMountType.Volume}:
        return False
    mount_path = mount.mount_path.rstrip("/")
    return mount_path == WORKER_USER_OUTPUT_VOLUME or mount_path.startswith(
        WORKER_USER_OUTPUT_VOLUME + "/"
    )


def volume_container_mount_paths(
    mount_path: str,
    *,
    fallback_name: str = "",
) -> tuple[str, str]:
    normalized = _clean_mount_path(mount_path, fallback_name=fallback_name)
    if _is_container_volume_path(normalized):
        return normalized, ""
    relative = normalized.lstrip("/")
    canonical = posixpath.join(WORKER_CONTAINER_VOLUME_PATH, relative)
    root = normalized if normalized.startswith("/") else ""
    return canonical, root


def _volume_request_mount(
    *,
    name: str,
    workspace_name: str,
    container_id: str,
    mount_path: str,
    link_path: str,
    read_only: bool,
    config: Mapping[str, JsonValue],
    local_path: str = "",
) -> RequestMount:
    if _is_external_volume_config(config):
        auth_mode = _mount_auth_mode(config)
        validate_mount_auth(
            auth_mode,
            _raw_text(config.get("access_key")),
            _raw_text(config.get("secret_key")),
        )
        return RequestMount(
            local_path=posixpath.join(DEFAULT_EXTERNAL_VOLUMES_PATH, workspace_name, name),
            mount_path=mount_path,
            link_path=link_path,
            read_only=read_only,
            mount_type=RequestMountType.MountPoint,
            mountpoint_config=RequestMountPointConfig(
                bucket_name=_raw_text(config.get("bucket_name")) or name,
                prefix=_raw_text(config.get("prefix")),
                auth_mode=auth_mode,
                endpoint_url=_raw_text(config.get("endpoint_url")),
                region=_raw_text(config.get("region")),
                force_path_style=_config_bool(config, "force_path_style"),
            ),
        )
    return RequestMount(
        local_path=local_path,
        mount_path=mount_path,
        link_path=link_path,
        read_only=read_only,
        mount_type=RequestMountType.Volume,
    )


def platform_volume_local_path(*, workspace_name: str, volume_id: str) -> str:
    """Logical path of a volume, which the worker resolves into workspace storage."""
    return posixpath.join(
        DEFAULT_VOLUMES_PATH,
        _namespace_segment(workspace_name, field="workspace_name"),
        _namespace_segment(volume_id, field="volume_id"),
    )


def _namespace_segment(value: str, *, field: str) -> str:
    normalized = value.strip()
    if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        msg = f"unsafe {field}: {value!r}"
        raise ValueError(msg)
    return normalized


def _volume_link_path(container_id: str, mount_path: str) -> str:
    if not container_id:
        return ""
    normalized = _clean_mount_path(mount_path)
    if normalized.startswith("/") and _is_container_volume_path(normalized):
        return ""
    relative = normalized.lstrip("/")
    if not relative:
        return ""
    return posixpath.join(SOURCE_WORKSPACE_ROOT.format(container_id=container_id), relative)


def _clean_mount_path(mount_path: str, *, fallback_name: str = "") -> str:
    raw = (mount_path or fallback_name).strip().replace("\\", "/")
    if not raw:
        raw = fallback_name or "."
    normalized = posixpath.normpath(raw)
    if normalized == ".":
        normalized = fallback_name or "."
    parts = [part for part in normalized.split("/") if part]
    if ".." in parts:
        msg = f"unsafe volume mount path: {mount_path}"
        raise ValueError(msg)
    return normalized


def _is_container_volume_path(mount_path: str) -> bool:
    root = WORKER_CONTAINER_VOLUME_PATH.rstrip("/")
    return mount_path == root or mount_path.startswith(root + "/")


def _is_external_volume_config(config: Mapping[str, JsonValue]) -> bool:
    return bool(_raw_text(config.get("bucket_name")))


def _mapping(raw: BaseModel | JsonValue) -> dict[str, JsonValue]:
    if isinstance(raw, BaseModel):
        return JSON_OBJECT_ADAPTER.validate_json(raw.model_dump_json())
    if not isinstance(raw, dict):
        return {}
    return raw


def _config_bool(config: Mapping[str, JsonValue], key: str) -> bool:
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return False


def _mount_auth_mode(config: Mapping[str, JsonValue]) -> MountAuthMode:
    value = config.get("auth_mode", MountAuthMode.Ambient)
    if isinstance(value, MountAuthMode):
        return value
    try:
        return MountAuthMode(_raw_text(value))
    except ValueError as exc:
        msg = f"unsupported mount auth mode: {value!r}"
        raise ValueError(msg) from exc


def _raw_text(value: JsonValue) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value)


__all__ = [
    "configured_volume_mounts",
    "container_resource_mounts",
    "container_resource_mounts_require_workspace_storage",
    "platform_volume_local_path",
    "source_code_mounts",
    "volume_container_mount_paths",
]

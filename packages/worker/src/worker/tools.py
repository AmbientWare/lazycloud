from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator
from shared.contracts import ContractModel
from shared.mounts import MountAuthMode, validate_mount_auth


class ContainerMountKind(StrEnum):
    Local = "local"
    MountPoint = "mountpoint"


class WorkspaceStorageCredentials(ContractModel):
    endpoint_url: str = ""
    region: str = ""
    bucket_name: str = ""
    prefix: str = ""
    access_key: str = ""
    secret_key: str = ""
    force_path_style: bool = False


class ContainerMount(ContractModel):
    mount_path: str
    bucket_name: str = ""
    kind: ContainerMountKind = ContainerMountKind.Local
    auth_mode: MountAuthMode = MountAuthMode.Ambient
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str = ""
    region: str = ""
    force_path_style: bool = False

    @model_validator(mode="after")
    def credentials_match_auth_mode(self) -> ContainerMount:
        validate_mount_auth(
            self.auth_mode,
            self.access_key,
            self.secret_key,
            allow_unhydrated_secret_references=True,
        )
        return self

    @property
    def credential_key(self) -> str:
        return f"{self.mount_path}:{self.bucket_name}"

    @property
    def needs_credentials(self) -> bool:
        return (
            self.kind == ContainerMountKind.MountPoint
            and self.bucket_name != ""
            and self.auth_mode is MountAuthMode.SecretReferences
            and self.access_key == ""
        )


class ContainerMountCredentialRequest(ContractModel):
    mount_path: str
    bucket_name: str

    @property
    def credential_key(self) -> str:
        return f"{self.mount_path}:{self.bucket_name}"


class ContainerMountCredentials(ContractModel):
    mount_path: str
    bucket_name: str
    access_key: str
    secret_key: str
    endpoint_url: str = ""
    region: str = ""
    force_path_style: bool = False

    @property
    def credential_key(self) -> str:
        return f"{self.mount_path}:{self.bucket_name}"


class ContainerCredentialContext(ContractModel):
    workspace_id: str
    stub_id: str
    container_id: str
    secret_names: list[str] = Field(default_factory=list)
    gateway_token_required: bool = False
    workspace_storage_required: bool = False
    mounts: list[ContainerMount] = Field(default_factory=list)


class ContainerCredentialRequest(ContractModel):
    workspace_id: str
    stub_id: str
    container_id: str
    secret_names: list[str] = Field(default_factory=list)
    gateway_token: bool = False
    workspace_storage: bool = False
    mount_credentials: list[ContainerMountCredentialRequest] = Field(default_factory=list)


class ContainerCredentials(ContractModel):
    env: list[str] = Field(default_factory=list)
    workspace_storage: WorkspaceStorageCredentials | None = None
    mount_credentials: list[ContainerMountCredentials] = Field(default_factory=list)


class ContainerCredentialApplication(ContractModel):
    env: list[str] = Field(default_factory=list)
    workspace_storage: WorkspaceStorageCredentials | None = None
    mounts: list[ContainerMount] = Field(default_factory=list)


class ProcessIoCounters(ContractModel):
    read_count: int = 0
    write_count: int = 0
    read_bytes: int = 0
    write_bytes: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0


class NetworkIoCounters(ContractModel):
    name: str = ""
    bytes_recv: int = 0
    bytes_sent: int = 0
    packets_recv: int = 0
    packets_sent: int = 0


def container_mount_credential_requests(
    mounts: list[ContainerMount],
) -> list[ContainerMountCredentialRequest]:
    seen: set[str] = set()
    requests: list[ContainerMountCredentialRequest] = []
    for mount in mounts:
        if not mount.needs_credentials or mount.credential_key in seen:
            continue
        seen.add(mount.credential_key)
        requests.append(
            ContainerMountCredentialRequest(
                mount_path=mount.mount_path,
                bucket_name=mount.bucket_name,
            )
        )
    return requests


def build_container_credential_request(
    context: ContainerCredentialContext,
) -> ContainerCredentialRequest:
    return ContainerCredentialRequest(
        workspace_id=context.workspace_id,
        stub_id=context.stub_id,
        container_id=context.container_id,
        secret_names=context.secret_names,
        gateway_token=context.gateway_token_required,
        workspace_storage=context.workspace_storage_required,
        mount_credentials=container_mount_credential_requests(context.mounts),
    )


def has_container_credential_request(request: ContainerCredentialRequest) -> bool:
    return (
        request.gateway_token
        or request.workspace_storage
        or bool(request.secret_names)
        or bool(request.mount_credentials)
    )


def env_key(item: str) -> str:
    key, separator, _ = item.partition("=")
    return key if separator else ""


def merge_credential_env(existing: list[str], vended: list[str]) -> list[str]:
    if not vended:
        return list(existing)
    vended_keys = {key for item in vended if (key := env_key(item))}
    merged = [item for item in existing if env_key(item) not in vended_keys]
    return [*merged, *vended]


def apply_container_credentials(
    *,
    existing_env: list[str],
    mounts: list[ContainerMount],
    credentials: ContainerCredentials,
) -> ContainerCredentialApplication:
    credential_by_key = {item.credential_key: item for item in credentials.mount_credentials}
    hydrated_mounts: list[ContainerMount] = []
    for mount in mounts:
        credential = credential_by_key.get(mount.credential_key)
        if credential is None:
            hydrated_mounts.append(mount)
            continue
        hydrated_mounts.append(
            mount.model_copy(
                update={
                    "access_key": credential.access_key,
                    "secret_key": credential.secret_key,
                    "endpoint_url": credential.endpoint_url,
                    "region": credential.region,
                    "force_path_style": credential.force_path_style,
                }
            )
        )
    return ContainerCredentialApplication(
        env=merge_credential_env(existing_env, credentials.env),
        workspace_storage=credentials.workspace_storage,
        mounts=hydrated_mounts,
    )


def process_io_delta(current: ProcessIoCounters, previous: ProcessIoCounters) -> ProcessIoCounters:
    return ProcessIoCounters(
        read_count=max(current.read_count - previous.read_count, 0),
        write_count=max(current.write_count - previous.write_count, 0),
        read_bytes=max(current.read_bytes - previous.read_bytes, 0),
        write_bytes=max(current.write_bytes - previous.write_bytes, 0),
        disk_read_bytes=max(current.disk_read_bytes - previous.disk_read_bytes, 0),
        disk_write_bytes=max(current.disk_write_bytes - previous.disk_write_bytes, 0),
    )


def aggregate_network_counters(
    counters: list[NetworkIoCounters],
    *,
    exclude_loopback: bool = True,
) -> NetworkIoCounters:
    ignored = {"lo", "lo0", "loopback"}
    selected = [
        item for item in counters if not exclude_loopback or item.name.lower() not in ignored
    ]
    return NetworkIoCounters(
        bytes_recv=sum(item.bytes_recv for item in selected),
        bytes_sent=sum(item.bytes_sent for item in selected),
        packets_recv=sum(item.packets_recv for item in selected),
        packets_sent=sum(item.packets_sent for item in selected),
    )


def network_io_delta(current: NetworkIoCounters, previous: NetworkIoCounters) -> NetworkIoCounters:
    return NetworkIoCounters(
        bytes_recv=max(current.bytes_recv - previous.bytes_recv, 0),
        bytes_sent=max(current.bytes_sent - previous.bytes_sent, 0),
        packets_recv=max(current.packets_recv - previous.packets_recv, 0),
        packets_sent=max(current.packets_sent - previous.packets_sent, 0),
    )

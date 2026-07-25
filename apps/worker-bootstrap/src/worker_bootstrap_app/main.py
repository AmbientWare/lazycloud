from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeGuard

from container_worker_app.production import (
    ProductionWorkerSettings,
    planned_scheduler_worker_record_from_settings,
)
from coordination.redis_client import RedisClient
from identity.auth import AuthError, AuthService, IdentityDatabaseContext
from identity.credential_files import CredentialFilePublication
from kubernetes import client, config
from kubernetes.client import ApiException
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from scheduler.state import (
    DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS,
    RedisSchedulerWorkerRepository,
)
from shared.app_identity import WORKER_BOOTSTRAP_PROCESS_NAME
from shared.identity import TokenKind
from worker.events import WorkerPoolMode
from worker.runtime_config import OciRuntimeName

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


class WorkerBootstrapArguments(argparse.Namespace):
    worker_id: str | None
    pool_name: str | None
    machine_id: str | None
    runtime: str | None
    pool_mode: str | None
    cpu_millicores: int | None
    memory_mib: int | None
    gpu_type: str | None
    gpu_count: int | None
    requires_pool_selector: bool | None
    preemptible: bool | None
    ttl_seconds: int


class WorkerTokenArguments(argparse.Namespace):
    name: str
    workspace: str
    output: Path


class KubernetesWorkerTokenArguments(argparse.Namespace):
    workspace: str
    attempts: int
    retry_delay_seconds: float


class _KubernetesConfigLoader(Protocol):
    def load_incluster_config(self) -> None: ...


class _KubernetesSecretApi(Protocol):
    def read_namespaced_secret(self, name: str, namespace: str) -> client.V1Secret: ...

    def create_namespaced_secret(
        self,
        namespace: str,
        body: client.V1Secret,
    ) -> client.V1Secret: ...

    def patch_namespaced_secret(
        self,
        name: str,
        namespace: str,
        body: client.V1Secret,
    ) -> client.V1Secret: ...


class _KubernetesSerializer(Protocol):
    def sanitize_for_serialization(self, response: client.V1Secret) -> JsonValue: ...


class _KubernetesSecretPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    data: dict[str, str] = Field(default_factory=dict)


def _is_kubernetes_config_loader(value: object) -> TypeGuard[_KubernetesConfigLoader]:
    return callable(getattr(value, "load_incluster_config", None))


def _is_kubernetes_secret_api(value: object) -> TypeGuard[_KubernetesSecretApi]:
    return all(
        callable(getattr(value, operation, None))
        for operation in (
            "read_namespaced_secret",
            "create_namespaced_secret",
            "patch_namespaced_secret",
        )
    )


def _is_kubernetes_serializer(value: object) -> TypeGuard[_KubernetesSerializer]:
    return callable(getattr(value, "sanitize_for_serialization", None))


def _api_exception_status(value: object) -> int | None:
    status = getattr(value, "status", None)
    return status if isinstance(status, int) else None


@dataclass(frozen=True)
class WorkerBootstrapResult:
    worker_id: str
    pool_name: str
    machine_id: str
    status: str
    ttl_seconds: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "worker_id": self.worker_id,
            "pool_name": self.pool_name,
            "machine_id": self.machine_id,
            "status": self.status,
            "ttl_seconds": self.ttl_seconds,
        }


@dataclass(frozen=True)
class KubernetesWorkerTokenSecretSettings:
    namespace: str
    secret_name: str
    secret_key: str
    token_name_prefix: str
    label_name: str
    label_instance: str
    label_managed_by: str
    workspace_id: str = "default"
    attempts: int = 60
    retry_delay_seconds: float = 2.0

    @classmethod
    def from_env(
        cls,
        *,
        workspace_id: str = "default",
        attempts: int = 60,
        retry_delay_seconds: float = 2.0,
    ) -> KubernetesWorkerTokenSecretSettings:
        return cls(
            namespace=_required_env("LAZYCLOUD_WORKER_TOKEN_SECRET_NAMESPACE"),
            secret_name=_required_env("LAZYCLOUD_WORKER_TOKEN_SECRET_NAME"),
            secret_key=_required_env("LAZYCLOUD_WORKER_TOKEN_SECRET_KEY"),
            token_name_prefix=_required_env("LAZYCLOUD_WORKER_TOKEN_NAME_PREFIX"),
            label_name=_required_env("LAZYCLOUD_APP_LABEL_NAME"),
            label_instance=_required_env("LAZYCLOUD_APP_LABEL_INSTANCE"),
            label_managed_by=_required_env("LAZYCLOUD_APP_LABEL_MANAGED_BY"),
            workspace_id=workspace_id,
            attempts=attempts,
            retry_delay_seconds=retry_delay_seconds,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=WORKER_BOOTSTRAP_PROCESS_NAME)
    parser.add_argument("--worker-id")
    parser.add_argument("--pool", dest="pool_name", default=None)
    parser.add_argument("--machine-id")
    parser.add_argument("--runtime", choices=[item.value for item in OciRuntimeName])
    parser.add_argument("--pool-mode", choices=[item.value for item in WorkerPoolMode])
    parser.add_argument("--cpu-millicores", type=int)
    parser.add_argument("--memory-mib", type=int)
    parser.add_argument("--gpu-type")
    parser.add_argument("--gpu-count", type=int)
    parser.add_argument("--requires-pool-selector", action="store_true", default=None)
    parser.add_argument("--preemptible", action="store_true", default=None)
    parser.add_argument("--ttl-seconds", type=int, default=DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS)
    return parser


def build_worker_token_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{WORKER_BOOTSTRAP_PROCESS_NAME} worker-token")
    parser.add_argument("name")
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def build_kubernetes_worker_token_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{WORKER_BOOTSTRAP_PROCESS_NAME} kubernetes-worker-token"
    )
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--attempts", type=int, default=60)
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    return parser


def bootstrap_scheduler_worker(
    *,
    settings: ProductionWorkerSettings | None = None,
    redis: RedisClient | None = None,
    ttl_seconds: int = DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS,
) -> WorkerBootstrapResult:
    config = settings or ProductionWorkerSettings()
    repository = RedisSchedulerWorkerRepository(redis or RedisClient.from_settings())
    worker = repository.add_worker(
        planned_scheduler_worker_record_from_settings(config),
        ttl_seconds=ttl_seconds,
    )
    return WorkerBootstrapResult(
        worker_id=worker.worker_id,
        pool_name=worker.pool_name,
        machine_id=worker.machine_id,
        status=worker.status.value,
        ttl_seconds=ttl_seconds,
    )


def create_worker_token(*, name: str, workspace_id: str = "default") -> str:
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.WorkerBootstrap)
    )
    try:
        raw_token, _record = AuthService(IdentityDatabaseContext(database)).create_service_token(
            name,
            kind=TokenKind.Worker,
            workspace_id=workspace_id,
        )
        return raw_token
    finally:
        database.dispose()


def write_worker_token(*, name: str, output: Path, workspace_id: str = "default") -> None:
    publication = CredentialFilePublication(
        output,
        f"service-token:{workspace_id}:{TokenKind.Worker.value}:{name}",
    )
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.WorkerBootstrap)
    )
    try:
        service = AuthService(IdentityDatabaseContext(database))
        if not service.administrator_ready():
            raise AuthError("offline administrator bootstrap must complete first")
        published = publication.read_published()
        if published is not None and _valid_worker_token(
            service,
            published,
            name=name,
            workspace_id=workspace_id,
        ):
            publication.secure_published_mode()
            if publication.read_staged() is not None:
                publication.discard_staged()
            return
        staged = publication.read_staged()
        if staged is not None:
            if _valid_worker_token(
                service,
                staged,
                name=name,
                workspace_id=workspace_id,
            ):
                publication.publish(replace=True)
                return
            publication.discard_staged()
        service.create_service_token(
            name,
            kind=TokenKind.Worker,
            workspace_id=workspace_id,
            stage_token=publication.stage,
        )
        publication.publish(replace=True)
    finally:
        database.dispose()


def ensure_kubernetes_worker_token_secret(
    settings: KubernetesWorkerTokenSecretSettings,
) -> str:
    config_loader: object = config
    if not _is_kubernetes_config_loader(config_loader):
        raise RuntimeError("Kubernetes in-cluster configuration loader is unavailable")
    config_loader.load_incluster_config()
    api_value: object = client.CoreV1Api()
    serializer_value: object = client.ApiClient()
    if not _is_kubernetes_secret_api(api_value):
        raise RuntimeError("Kubernetes Secret API is incomplete")
    if not _is_kubernetes_serializer(serializer_value):
        raise RuntimeError("Kubernetes API serializer is incomplete")
    token_name = f"{settings.token_name_prefix}-{settings.label_instance}"
    existing_token = _kubernetes_secret_token(api_value, serializer_value, settings)
    if existing_token is not None and _worker_token_is_valid(
        existing_token,
        name=token_name,
        workspace_id=settings.workspace_id,
    ):
        return "worker token secret already initialized"

    last_error: Exception | None = None
    for attempt in range(1, max(settings.attempts, 1) + 1):
        try:
            token = create_worker_token(name=token_name, workspace_id=settings.workspace_id)
            _upsert_kubernetes_worker_token_secret(api_value, settings, token)
            return "worker token secret initialized"
        except Exception as exc:
            last_error = exc
            if attempt >= settings.attempts:
                break
            time.sleep(max(settings.retry_delay_seconds, 0.0))

    msg = "failed to initialize worker token secret"
    if last_error is not None:
        raise RuntimeError(msg) from last_error
    raise RuntimeError(msg)


def main(argv: list[str] | None = None) -> None:
    args_list = list(argv) if argv is not None else None
    selected = args_list if args_list is not None else sys.argv[1:]
    if selected and selected[0] == "worker-token":
        args = build_worker_token_parser().parse_args(
            selected[1:], namespace=WorkerTokenArguments()
        )
        write_worker_token(name=args.name, output=args.output, workspace_id=args.workspace)
        print("worker token initialized")
        return
    if selected and selected[0] == "kubernetes-worker-token":
        args = build_kubernetes_worker_token_parser().parse_args(
            selected[1:], namespace=KubernetesWorkerTokenArguments()
        )
        result = ensure_kubernetes_worker_token_secret(
            KubernetesWorkerTokenSecretSettings.from_env(
                workspace_id=args.workspace,
                attempts=args.attempts,
                retry_delay_seconds=args.retry_delay_seconds,
            )
        )
        print(result)
        return

    args = build_parser().parse_args(args_list, namespace=WorkerBootstrapArguments())
    result = bootstrap_scheduler_worker(
        settings=_settings_from_args(args),
        ttl_seconds=args.ttl_seconds,
    )
    print(json.dumps(result.to_dict(), sort_keys=True))


def _settings_from_args(args: WorkerBootstrapArguments) -> ProductionWorkerSettings:
    base = ProductionWorkerSettings()
    return base.model_copy(
        update={
            "worker_id": args.worker_id if args.worker_id is not None else base.worker_id,
            "pool_name": args.pool_name if args.pool_name is not None else base.pool_name,
            "machine_id": args.machine_id if args.machine_id is not None else base.machine_id,
            "runtime": OciRuntimeName(args.runtime) if args.runtime is not None else base.runtime,
            "pool_mode": (
                WorkerPoolMode(args.pool_mode) if args.pool_mode is not None else base.pool_mode
            ),
            "cpu_millicores": (
                args.cpu_millicores if args.cpu_millicores is not None else base.cpu_millicores
            ),
            "memory_mib": args.memory_mib if args.memory_mib is not None else base.memory_mib,
            "gpu_type": args.gpu_type if args.gpu_type is not None else base.gpu_type,
            "gpu_count": args.gpu_count if args.gpu_count is not None else base.gpu_count,
            "requires_pool_selector": (
                args.requires_pool_selector
                if args.requires_pool_selector is not None
                else base.requires_pool_selector
            ),
            "preemptible": (args.preemptible if args.preemptible is not None else base.preemptible),
        }
    )


def _kubernetes_secret_token(
    api: _KubernetesSecretApi,
    serializer: _KubernetesSerializer,
    settings: KubernetesWorkerTokenSecretSettings,
) -> str | None:
    try:
        secret = api.read_namespaced_secret(settings.secret_name, settings.namespace)
    except ApiException as exc:
        if _api_exception_status(exc) == 404:
            return None
        raise
    if not isinstance(secret, client.V1Secret):
        raise RuntimeError("Kubernetes API returned an invalid Secret response")
    data = _kubernetes_secret_data(secret, serializer)
    encoded = data.get(settings.secret_key)
    if not encoded:
        return None
    try:
        token = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Kubernetes worker token Secret contains invalid data") from exc
    return token or None


def _worker_token_is_valid(token: str, *, name: str, workspace_id: str) -> bool:
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.WorkerBootstrap)
    )
    try:
        return _valid_worker_token(
            AuthService(IdentityDatabaseContext(database)),
            token,
            name=name,
            workspace_id=workspace_id,
        )
    finally:
        database.dispose()


def _valid_worker_token(
    service: AuthService,
    token: str,
    *,
    name: str,
    workspace_id: str,
) -> bool:
    try:
        service.validate_service_token(
            token,
            name=name,
            kind=TokenKind.Worker,
            workspace_id=workspace_id,
        )
    except AuthError:
        return False
    return True


def _upsert_kubernetes_worker_token_secret(
    api: _KubernetesSecretApi,
    settings: KubernetesWorkerTokenSecretSettings,
    token: str,
) -> None:
    if not token:
        msg = "worker token is empty"
        raise RuntimeError(msg)
    encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=settings.secret_name,
            labels={
                "app.kubernetes.io/name": settings.label_name,
                "app.kubernetes.io/instance": settings.label_instance,
                "app.kubernetes.io/managed-by": settings.label_managed_by,
                "app.kubernetes.io/component": "worker-token",
            },
        ),
        type="Opaque",
        data={settings.secret_key: encoded},
    )
    try:
        api.create_namespaced_secret(settings.namespace, secret)
    except ApiException as exc:
        if _api_exception_status(exc) != 409:
            raise
        api.patch_namespaced_secret(settings.secret_name, settings.namespace, secret)


def _kubernetes_secret_data(
    secret: client.V1Secret,
    serializer: _KubernetesSerializer,
) -> dict[str, str]:
    payload = serializer.sanitize_for_serialization(secret)
    return _KubernetesSecretPayload.model_validate(payload).data


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value:
        return value
    msg = f"{name} is required"
    raise RuntimeError(msg)


if __name__ == "__main__":
    main()

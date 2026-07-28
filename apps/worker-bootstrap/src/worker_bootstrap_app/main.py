from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from container_worker_app.production import (
    ProductionWorkerSettings,
    planned_scheduler_worker_record_from_settings,
)
from coordination.redis_client import RedisClient
from identity.auth import AuthError, AuthService, IdentityDatabaseContext
from identity.credential_files import CredentialFilePublication
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


if __name__ == "__main__":
    main()

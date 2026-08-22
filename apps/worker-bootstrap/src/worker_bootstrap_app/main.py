from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from container_worker_app.composition import planned_scheduler_worker_record_from_settings
from container_worker_app.settings import WorkerSettings
from coordination.redis_client import RedisClient
from identity.auth import AuthError, AuthService, IdentityDatabaseContext
from identity.credential_files import CredentialFilePublication
from scheduler.state import (
    DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS,
    RedisSchedulerWorkerRepository,
)
from shared.app_identity import WORKER_BOOTSTRAP_PROCESS_NAME
from shared.compute_policy import MachinePool
from shared.identity import AuthScope, TokenKind

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


class WorkerBootstrapArguments(argparse.Namespace):
    """Which worker to register, not what capacity it has.

    Capacity, runtimes, and pool mode come from the worker configuration file
    this machine already runs its worker from, so the record registered here
    cannot disagree with the worker that later claims it.
    """

    worker_id: str | None
    pool: MachinePool | None
    machine_id: str | None
    ttl_seconds: int


class WorkerTokenArguments(argparse.Namespace):
    name: str
    workspace: str
    output: Path


@dataclass(frozen=True)
class WorkerBootstrapResult:
    worker_id: str
    pool: MachinePool
    machine_id: str
    status: str
    ttl_seconds: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "worker_id": self.worker_id,
            "pool": self.pool,
            "machine_id": self.machine_id,
            "status": self.status,
            "ttl_seconds": self.ttl_seconds,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=WORKER_BOOTSTRAP_PROCESS_NAME)
    parser.add_argument("--worker-id")
    parser.add_argument("--pool", dest="pool", default=None)
    parser.add_argument("--machine-id")
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
    settings: WorkerSettings | None = None,
    redis: RedisClient | None = None,
    ttl_seconds: int = DEFAULT_PENDING_WORKER_STATE_TTL_SECONDS,
) -> WorkerBootstrapResult:
    config = settings or WorkerSettings()
    repository = RedisSchedulerWorkerRepository(redis or RedisClient.from_settings())
    worker = repository.add_worker(
        planned_scheduler_worker_record_from_settings(config),
        ttl_seconds=ttl_seconds,
    )
    return WorkerBootstrapResult(
        worker_id=worker.worker_id,
        pool=worker.pool,
        machine_id=worker.machine_id,
        status=worker.status.value,
        ttl_seconds=ttl_seconds,
    )


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
            scopes=[AuthScope.Worker.value],
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


def _settings_from_args(args: WorkerBootstrapArguments) -> WorkerSettings:
    loaded = WorkerSettings()
    return WorkerSettings(
        worker_id=_override(args.worker_id, loaded.worker_id),
        pool=_override(args.pool, loaded.pool),
        machine_id=_override(args.machine_id, loaded.machine_id),
    )


def _override[T](value: T | None, loaded: T) -> T:
    """An argument this invocation passed, or what the settings sources produced.

    Only the argument parser can say "absent"; every other layering already
    happened in the settings sources.
    """
    return loaded if value is None else value


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
            scopes=[AuthScope.Worker.value],
        )
    except AuthError:
        return False
    return True


if __name__ == "__main__":
    main()

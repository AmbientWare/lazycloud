from __future__ import annotations

import argparse
import threading
from collections.abc import Callable
from pathlib import Path

from foundation.process_logs import configure_process_logging
from gateway.settings import GatewaySettings
from images.settings import ImageBuildContainerSettings
from observability.settings import (
    TelemetrySettings,
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.telemetry import setup_telemetry
from scheduler.reconciliation import DEFAULT_AUTOSCALING_RECONCILE_LIMIT, SchedulerRunResult
from shared.app_identity import SCHEDULER_PROCESS_NAME
from shared.process_liveness import HeartbeatFile, heartbeat_path
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage_client.s3 import S3ObjectStoreSettings

from scheduler_app.composition_settings import (
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)
from scheduler_app.health import scheduler_heartbeat_paths
from scheduler_app.loops import (
    LOOP_SHUTDOWN_TIMEOUT_SECONDS,
    LOOP_SUPERVISOR_POLL_SECONDS,
    scheduler_shutdown_handlers,
    start_scheduler_loops,
)
from scheduler_app.runtime import SchedulerRuntime
from scheduler_app.settings import SchedulerProcessSettings


class SchedulerCommandArgs(argparse.Namespace):
    container_limit: int
    autoscaling_limit: int
    once: bool
    include_cron_jobs: bool
    include_containers: bool
    heartbeat_file: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=SCHEDULER_PROCESS_NAME)
    parser.add_argument("--container-limit", type=int, default=100)
    parser.add_argument(
        "--autoscaling-limit",
        type=int,
        default=DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--cron-jobs", dest="include_cron_jobs", action="store_true", default=True)
    parser.add_argument("--no-cron-jobs", dest="include_cron_jobs", action="store_false")
    parser.add_argument(
        "--containers", dest="include_containers", action="store_true", default=True
    )
    parser.add_argument("--no-containers", dest="include_containers", action="store_false")
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        default=heartbeat_path(SCHEDULER_PROCESS_NAME),
        help="loop-progress heartbeat file touched once per iteration",
    )
    return parser


def parse_scheduler_args(argv: list[str] | None = None) -> SchedulerCommandArgs:
    return build_parser().parse_args(argv, namespace=SchedulerCommandArgs())


def run_scheduler(
    *,
    runtime: SchedulerRuntime,
    container_limit: int = 100,
    autoscaling_limit: int = DEFAULT_AUTOSCALING_RECONCILE_LIMIT,
    once: bool = False,
    include_cron_jobs: bool = True,
    include_containers: bool = True,
    heartbeat_file: Path | None = None,
) -> SchedulerRunResult | None:
    with runtime:
        if once:
            result = runtime.scheduler.run_once(
                include_cron_jobs=include_cron_jobs,
                include_containers=include_containers,
                container_limit=container_limit,
                autoscaling_limit=autoscaling_limit,
            )
            return result
        stop = threading.Event()
        beats = _loop_heartbeats(heartbeat_file)
        with scheduler_shutdown_handlers(stop):
            supervisor = start_scheduler_loops(
                runtime.scheduler,
                include_cron_jobs=include_cron_jobs,
                include_containers=include_containers,
                container_limit=container_limit,
                autoscaling_limit=autoscaling_limit,
                beats=beats,
                stop=stop,
            )
            try:
                # The signal handler sets the event; nothing else ends the
                # process. Waiting on it rather than on the threads keeps the
                # main thread free to take the signal at all.
                while not stop.wait(LOOP_SUPERVISOR_POLL_SECONDS):
                    pass
            finally:
                supervisor.shutdown(timeout_seconds=LOOP_SHUTDOWN_TIMEOUT_SECONDS)
    return None


def _loop_heartbeats(heartbeat_file: Path | None) -> dict[str, Callable[[], None]]:
    if heartbeat_file is None:
        return {}
    return {
        name: HeartbeatFile(path).beat
        for name, path in scheduler_heartbeat_paths(heartbeat_file).items()
    }


def build_scheduler_runtime(
    *,
    public_gateway_http_url: str,
) -> SchedulerRuntime:
    scheduler_settings = SchedulerProcessSettings()
    object_store_settings = S3ObjectStoreSettings()
    return SchedulerRuntime.create(
        public_gateway_http_url=public_gateway_http_url,
        observability=SchedulerObservabilitySettings(
            workspace_changes=WorkspaceChangeStreamSettings(),
        ),
        storage=SchedulerStorageSettings(
            object_store=object_store_settings,
            image_archive=ImageArchiveSettings(bucket=object_store_settings.bucket),
            retention=RetentionSettings(),
            volume_metering=VolumeMeteringSettings(),
            workload_image_registry_repository=(
                scheduler_settings.workload_image_registry_repository
            ),
        ),
        image_build_container_settings=ImageBuildContainerSettings(),
    )


def main(argv: list[str] | None = None) -> None:
    # First, because everything this process reports about what it reconciled,
    # skipped or declined goes through the root logger.
    configure_process_logging()
    args = parse_scheduler_args(argv)
    gateway_settings = GatewaySettings()
    # The autoscalers record here, not in the API, so this process needs its own
    # meter provider. Without one every `autoscaler_*` and `worker_pool_*` metric
    # is written to a no-op instrument and never leaves the host.
    telemetry = setup_telemetry(TelemetrySettings().to_config(service_name=SCHEDULER_PROCESS_NAME))
    try:
        result = run_scheduler(
            runtime=build_scheduler_runtime(
                public_gateway_http_url=gateway_settings.public_http_url,
            ),
            container_limit=args.container_limit,
            autoscaling_limit=args.autoscaling_limit,
            once=args.once,
            include_cron_jobs=args.include_cron_jobs,
            include_containers=args.include_containers,
            heartbeat_file=args.heartbeat_file,
        )
    finally:
        # Flushes what the last interval has not exported yet, which is most of a
        # `--once` run.
        telemetry.shutdown()
    if result is not None:
        print(result.model_dump_json(exclude_defaults=True))


if __name__ == "__main__":
    main()

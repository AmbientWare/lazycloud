from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from compute.reclaim import ComputeReclaimSettings
from foundation.process_logs import configure_process_logging
from gateway.settings import GatewaySettings
from observability.settings import (
    TelemetrySettings,
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from observability.telemetry import setup_telemetry
from provider_clients.release import resolve_deployment_release
from shared.process_liveness import HeartbeatFile, heartbeat_path
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage_client.s3 import S3ObjectStoreSettings

from scheduler_app.composition_settings import (
    SchedulerCapacitySettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)
from scheduler_app.fleet_health import FLEET_PROCESS_NAME, fleet_heartbeat_paths
from scheduler_app.fleet_loops import start_fleet_loops
from scheduler_app.fleet_runtime import FleetRuntime
from scheduler_app.loops import LOOP_SHUTDOWN_TIMEOUT_SECONDS, scheduler_shutdown_handlers
from scheduler_app.settings import SchedulerProcessSettings


def build_fleet_runtime() -> FleetRuntime:
    settings = SchedulerProcessSettings()
    release = resolve_deployment_release()
    print(f"fleet-controller {release.describe()}", file=sys.stderr, flush=True)
    storage = S3ObjectStoreSettings()
    return FleetRuntime.create(
        public_gateway_http_url=GatewaySettings().public_http_url,
        observability=SchedulerObservabilitySettings(
            workspace_changes=WorkspaceChangeStreamSettings()
        ),
        storage=SchedulerStorageSettings(
            object_store=storage,
            image_archive=ImageArchiveSettings(bucket=storage.bucket),
            retention=RetentionSettings(),
            volume_metering=VolumeMeteringSettings(),
            workload_image_registry_repository=settings.workload_image_registry_repository,
        ),
        capacity=SchedulerCapacitySettings(
            aws_connections=release.aws_connections,
            aws_capacity=release.aws_capacity,
            agent_binaries=release.agent_binaries,
            reclaim=ComputeReclaimSettings().to_policy(),
        ),
        managed_compute_reconcile_interval_seconds=settings.managed_compute_reconcile_interval_seconds,
    )


class FleetArguments(argparse.Namespace):
    once: bool
    container_limit: int
    heartbeat_file: Path


def main(argv: list[str] | None = None) -> None:
    configure_process_logging()
    parser = argparse.ArgumentParser(prog=FLEET_PROCESS_NAME)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--container-limit", type=int, default=100)
    parser.add_argument("--heartbeat-file", type=Path, default=heartbeat_path(FLEET_PROCESS_NAME))
    args = parser.parse_args(argv, namespace=FleetArguments())
    telemetry = setup_telemetry(TelemetrySettings().to_config(service_name=FLEET_PROCESS_NAME))
    try:
        with build_fleet_runtime() as runtime:
            if args.once:
                print(
                    runtime.controller.run_once(
                        container_limit=args.container_limit
                    ).model_dump_json(exclude_defaults=True)
                )
                return
            stop = threading.Event()
            beats = {
                name: HeartbeatFile(path).beat
                for name, path in fleet_heartbeat_paths(args.heartbeat_file).items()
            }
            with scheduler_shutdown_handlers(stop):
                supervisor = start_fleet_loops(
                    runtime.controller, stop=stop, beats=beats, container_limit=args.container_limit
                )
                try:
                    while not stop.wait(1.0):
                        pass
                finally:
                    supervisor.shutdown(timeout_seconds=LOOP_SHUTDOWN_TIMEOUT_SECONDS)
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    main()

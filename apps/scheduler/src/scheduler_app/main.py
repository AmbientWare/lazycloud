from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from compute.reclaim import ComputeReclaimSettings
from gateway.settings import GatewaySettings
from images.settings import ImageBuildContainerSettings
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from observability.settings import (
    VolumeMeteringSettings,
    WorkspaceChangeStreamSettings,
)
from provider_clients.release import resolve_deployment_release
from shared.app_identity import SCHEDULER_PROCESS_NAME
from shared.process_liveness import HeartbeatFile, heartbeat_path
from storage.image_archive import ImageArchiveSettings
from storage.retention_settings import RetentionSettings
from storage_client.s3 import S3ObjectStoreSettings

from scheduler_app.runtime import SchedulerRuntime
from scheduler_app.services import (
    SchedulerCapacitySettings,
    SchedulerNetworkSettings,
    SchedulerObservabilitySettings,
    SchedulerStorageSettings,
)
from scheduler_app.settings import SchedulerProcessSettings


class SchedulerCommandArgs(argparse.Namespace):
    interval_seconds: float
    container_limit: int
    once: bool
    include_cron_jobs: bool
    include_containers: bool
    heartbeat_file: Path


@dataclass(frozen=True)
class SchedulerProcessResult:
    cron_job_run_count: int
    function_retry_count: int
    container_dispatch_count: int
    dispatched_container_count: int
    app_lifecycle_reconcile_count: int = 0
    task_queue_autoscale_count: int = 0
    task_queue_autoscale_action_count: int = 0
    endpoint_autoscale_count: int = 0
    endpoint_autoscale_action_count: int = 0
    pod_autoscale_count: int = 0
    pod_autoscale_action_count: int = 0
    agent_pool_reconcile_count: int = 0
    agent_pool_touched_count: int = 0
    pool_state_count: int = 0
    worker_pool_drain_count: int = 0
    worker_pool_drain_action_count: int = 0
    worker_cleanup_count: int = 0
    orphaned_container_failure_count: int = 0
    volume_metering_count: int = 0
    volume_metering_failure_count: int = 0
    meter_events_sent_count: int = 0
    meter_events_retried_count: int = 0
    meter_events_abandoned_count: int = 0
    meter_events_abandoned_outstanding_count: int = 0
    meter_events_abandoned_outstanding_nanos: int = 0
    meter_events_pruned: int = 0
    plan_changes_applied_count: int = 0
    plan_changes_not_applied_count: int = 0
    plan_changes_retried_count: int = 0
    plan_changes_abandoned_count: int = 0
    plan_changes_open_count: int = 0
    billing_reconcile_checked_count: int = 0
    billing_reconcile_divergent_count: int = 0
    billing_reconcile_failure_count: int = 0
    billing_enforcement_unfunded_count: int = 0
    billing_enforcement_stopped_count: int = 0
    billing_enforcement_failure_count: int = 0
    objects_removed: int = 0
    retention_failure_count: int = 0
    tailnet_cleanup_processed_count: int = 0
    tailnet_cleanup_completed_count: int = 0
    tailnet_cleanup_failure_count: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cron_job_run_count": self.cron_job_run_count,
            "function_retry_count": self.function_retry_count,
            "app_lifecycle_reconcile_count": self.app_lifecycle_reconcile_count,
            "task_queue_autoscale_count": self.task_queue_autoscale_count,
            "task_queue_autoscale_action_count": self.task_queue_autoscale_action_count,
            "endpoint_autoscale_count": self.endpoint_autoscale_count,
            "endpoint_autoscale_action_count": self.endpoint_autoscale_action_count,
            "pod_autoscale_count": self.pod_autoscale_count,
            "pod_autoscale_action_count": self.pod_autoscale_action_count,
            "agent_pool_reconcile_count": self.agent_pool_reconcile_count,
            "agent_pool_touched_count": self.agent_pool_touched_count,
            "pool_state_count": self.pool_state_count,
            "worker_pool_drain_count": self.worker_pool_drain_count,
            "worker_pool_drain_action_count": self.worker_pool_drain_action_count,
            "worker_cleanup_count": self.worker_cleanup_count,
            "orphaned_container_failure_count": self.orphaned_container_failure_count,
            "volume_metering_count": self.volume_metering_count,
            "volume_metering_failure_count": self.volume_metering_failure_count,
            "meter_events_sent_count": self.meter_events_sent_count,
            "meter_events_retried_count": self.meter_events_retried_count,
            "meter_events_abandoned_count": self.meter_events_abandoned_count,
            "meter_events_abandoned_outstanding_count": (
                self.meter_events_abandoned_outstanding_count
            ),
            "meter_events_abandoned_outstanding_nanos": (
                self.meter_events_abandoned_outstanding_nanos
            ),
            "meter_events_pruned": self.meter_events_pruned,
            "plan_changes_applied_count": self.plan_changes_applied_count,
            "plan_changes_not_applied_count": self.plan_changes_not_applied_count,
            "plan_changes_retried_count": self.plan_changes_retried_count,
            "plan_changes_abandoned_count": self.plan_changes_abandoned_count,
            "plan_changes_open_count": self.plan_changes_open_count,
            "billing_reconcile_checked_count": self.billing_reconcile_checked_count,
            "billing_reconcile_divergent_count": self.billing_reconcile_divergent_count,
            "billing_reconcile_failure_count": self.billing_reconcile_failure_count,
            "billing_enforcement_unfunded_count": self.billing_enforcement_unfunded_count,
            "billing_enforcement_stopped_count": self.billing_enforcement_stopped_count,
            "billing_enforcement_failure_count": self.billing_enforcement_failure_count,
            "objects_removed": self.objects_removed,
            "retention_failure_count": self.retention_failure_count,
            "tailnet_cleanup_processed_count": self.tailnet_cleanup_processed_count,
            "tailnet_cleanup_completed_count": self.tailnet_cleanup_completed_count,
            "tailnet_cleanup_failure_count": self.tailnet_cleanup_failure_count,
            "container_dispatch_count": self.container_dispatch_count,
            "dispatched_container_count": self.dispatched_container_count,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=SCHEDULER_PROCESS_NAME)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--container-limit", type=int, default=100)
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
    interval_seconds: float = 1.0,
    container_limit: int = 100,
    once: bool = False,
    include_cron_jobs: bool = True,
    include_containers: bool = True,
    heartbeat_file: Path | None = None,
) -> SchedulerProcessResult | None:
    runtime.scheduler.interval_seconds = interval_seconds
    with runtime:
        if once:
            result = runtime.scheduler.run_once(
                include_cron_jobs=include_cron_jobs,
                include_containers=include_containers,
                container_limit=container_limit,
            )
            return SchedulerProcessResult(
                cron_job_run_count=len(result.cron_job_runs),
                function_retry_count=len(result.function_retries),
                app_lifecycle_reconcile_count=len(result.app_lifecycle_reconciliations),
                container_dispatch_count=len(result.container_dispatches),
                dispatched_container_count=sum(
                    1 for dispatch in result.container_dispatches if dispatch.dispatched
                ),
                task_queue_autoscale_count=len(result.task_queue_autoscaling),
                task_queue_autoscale_action_count=sum(
                    len(item.actions) for item in result.task_queue_autoscaling
                ),
                endpoint_autoscale_count=len(result.endpoint_autoscaling),
                endpoint_autoscale_action_count=sum(
                    len(item.actions) for item in result.endpoint_autoscaling
                ),
                pod_autoscale_count=len(result.pod_autoscaling),
                pod_autoscale_action_count=sum(
                    len(item.actions) for item in result.pod_autoscaling
                ),
                agent_pool_reconcile_count=len(result.agent_pool_reconciliations),
                agent_pool_touched_count=sum(
                    reconciliation.touched_count
                    for reconciliation in result.agent_pool_reconciliations
                ),
                pool_state_count=len(result.pool_states),
                worker_pool_drain_count=len(result.worker_pool_drains),
                worker_pool_drain_action_count=sum(
                    1 for item in result.worker_pool_drains if item.action != "none"
                ),
                worker_cleanup_count=len(result.worker_cleanups),
                orphaned_container_failure_count=len(result.orphaned_containers_failed),
                volume_metering_count=result.volume_metering_count,
                volume_metering_failure_count=result.volume_metering_failure_count,
                meter_events_sent_count=result.meter_events_sent_count,
                meter_events_retried_count=result.meter_events_retried_count,
                meter_events_abandoned_count=result.meter_events_abandoned_count,
                meter_events_abandoned_outstanding_count=(
                    result.meter_events_abandoned_outstanding_count
                ),
                meter_events_abandoned_outstanding_nanos=(
                    result.meter_events_abandoned_outstanding_nanos
                ),
                meter_events_pruned=result.meter_events_pruned,
                plan_changes_applied_count=result.plan_changes_applied_count,
                plan_changes_not_applied_count=result.plan_changes_not_applied_count,
                plan_changes_retried_count=result.plan_changes_retried_count,
                plan_changes_abandoned_count=result.plan_changes_abandoned_count,
                plan_changes_open_count=result.plan_changes_open_count,
                billing_reconcile_checked_count=result.billing_reconcile_checked_count,
                billing_reconcile_divergent_count=result.billing_reconcile_divergent_count,
                billing_reconcile_failure_count=result.billing_reconcile_failure_count,
                billing_enforcement_unfunded_count=result.billing_enforcement_unfunded_count,
                billing_enforcement_stopped_count=result.billing_enforcement_stopped_count,
                billing_enforcement_failure_count=result.billing_enforcement_failure_count,
                objects_removed=result.objects_removed,
                retention_failure_count=result.retention_failure_count,
                tailnet_cleanup_processed_count=result.tailnet_cleanup_processed_count,
                tailnet_cleanup_completed_count=result.tailnet_cleanup_completed_count,
                tailnet_cleanup_failure_count=result.tailnet_cleanup_failure_count,
            )
        heartbeat = None if heartbeat_file is None else HeartbeatFile(heartbeat_file)
        runtime.scheduler.run_forever(
            include_cron_jobs=include_cron_jobs,
            include_containers=include_containers,
            container_limit=container_limit,
            beat=None if heartbeat is None else heartbeat.beat,
        )
    return None


def build_scheduler_runtime(
    *,
    public_gateway_http_url: str,
    runtime_callback_http_url: str,
    interval_seconds: float = 1.0,
) -> SchedulerRuntime:
    scheduler_settings = SchedulerProcessSettings()
    # The API launches nodes from the same release facts; a scheduler resolving a
    # different one shows up as launch templates alternating between versions.
    release = resolve_deployment_release()
    print(f"scheduler {release.describe()}", file=sys.stderr, flush=True)
    return SchedulerRuntime.create(
        public_gateway_http_url=public_gateway_http_url,
        runtime_callback_http_url=runtime_callback_http_url,
        observability=SchedulerObservabilitySettings(
            workspace_changes=WorkspaceChangeStreamSettings(),
        ),
        storage=SchedulerStorageSettings(
            object_store=S3ObjectStoreSettings(),
            image_archive=ImageArchiveSettings(),
            retention=RetentionSettings(),
            volume_metering=VolumeMeteringSettings(),
        ),
        network=SchedulerNetworkSettings(
            tailnet_runtime=TailnetRuntimeSettings(),
            tailnet_control=TailnetControlSettings(),
            backend_routes=BackendRouteSettings(),
        ),
        capacity=SchedulerCapacitySettings(
            aws_connections=release.aws_connections,
            aws_capacity=release.aws_capacity,
            agent_binaries=release.agent_binaries,
            reclaim=ComputeReclaimSettings().to_policy(),
        ),
        interval_seconds=interval_seconds,
        managed_compute_reconcile_interval_seconds=(
            scheduler_settings.managed_compute_reconcile_interval_seconds
        ),
        image_build_container_settings=ImageBuildContainerSettings(),
    )


def main(argv: list[str] | None = None) -> None:
    args = parse_scheduler_args(argv)
    gateway_settings = GatewaySettings()
    result = run_scheduler(
        runtime=build_scheduler_runtime(
            public_gateway_http_url=gateway_settings.public_http_url,
            runtime_callback_http_url=gateway_settings.runtime_callback_http_url,
            interval_seconds=args.interval_seconds,
        ),
        interval_seconds=args.interval_seconds,
        container_limit=args.container_limit,
        once=args.once,
        include_cron_jobs=args.include_cron_jobs,
        include_containers=args.include_containers,
        heartbeat_file=args.heartbeat_file,
    )
    if result is not None:
        print(json.dumps(result.to_dict(), sort_keys=True))


if __name__ == "__main__":
    main()

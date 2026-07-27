from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Protocol, runtime_checkable

from shared.app_identity import CONTAINER_WORKER_PROCESS_NAME
from shared.container_requests import StopContainerReason
from shared.process_liveness import HeartbeatFile, heartbeat_path
from shared.routing import BackendRouteTransport
from storage_client.mounts import StorageMountMode
from worker.cache_assets import WorkerStorageMode
from worker.events import WorkerPoolMode, WorkerStreamEventKind
from worker.repository_payloads import StreamWorkerEventsRequest
from worker.runtime_config import OciRuntimeName
from worker.scheduler_requests import WorkerSchedulerRequestResult
from worker.worker_lifecycle import (
    DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS,
    WorkerLifecycleAction,
    WorkerLifecycleStatus,
    WorkerLifecycleStepResult,
)

from container_worker_app.production import ProductionWorkerSettings
from container_worker_app.runtime import (
    ContainerWorkerEventHandler,
    ContainerWorkerEventSource,
    ContainerWorkerRuntime,
    ContainerWorkerServices,
)

from .container_service_http import (
    ContainerServiceHttpServer,
    ContainerServiceRequestHandler,
    start_container_service_http_server,
)

WORKER_EVENT_POLL_INTERVAL_SECONDS = 0.1
WORKER_EVENT_HEARTBEAT_INTERVAL_SECONDS = 0.2
WORKER_EVENT_PUBSUB_TIMEOUT_SECONDS = 0.05
WORKER_EVENT_BATCH_SIZE = 1
DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS = 15.0
MAX_WORKER_KEEPALIVE_INTERVAL_SECONDS = DEFAULT_WORKER_KEEPALIVE_TTL_SECONDS / 3


class ContainerWorkerArguments(argparse.Namespace):
    worker_id: str | None = None
    pool_name: str | None = None
    machine_id: str | None = None
    pod_address: str | None = None
    container_service_port: int | None = None
    runtime: str | None = None
    pool_mode: str | None = None
    worker_repository_url: str | None = None
    worker_token: str | None = None
    worker_repository_timeout_seconds: float | None = None
    interval_seconds: float = 0.1
    keepalive_interval_seconds: float = DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS
    heartbeat_file: Path = heartbeat_path(CONTAINER_WORKER_PROCESS_NAME)
    worker_spindown_seconds: float | None = None
    metrics_interval_seconds: float | None = None
    metrics_enabled: bool | None = None
    container_cost_hook_endpoint: str | None = None
    container_cost_hook_token: str | None = None
    container_cost_hook_timeout_seconds: float | None = None
    gpu_devices: str | None = None
    nvidia_cdi_enabled: bool | None = None
    once: bool = False
    persistent: bool | None = None
    agent_worker: bool | None = None
    agent_bridge_network: bool | None = None
    network_prefix: str | None = None
    route_transport: str | None = None
    route_local_target_host: str | None = None
    bundle_root: Path | None = None
    image_cache_path: str | None = None
    image_mount_root: str | None = None
    image_archive_extension: str | None = None
    cache_root: Path | None = None
    checkpoint_root: str | None = None
    checkpoint_bucket: str | None = None
    data_storage_mode: str | None = None
    data_storage_path: str | None = None
    data_storage_bucket: str | None = None
    data_storage_juicefs_redis_url: str | None = None
    workspace_storage_mode: str | None = None
    workspace_storage_base_mount_path: str | None = None
    workspace_storage_mountpoint_binary: str | None = None


@runtime_checkable
class ContainerWorkerTransportServices(Protocol):
    @property
    def container_transport(self) -> ContainerServiceRequestHandler: ...


class ContainerWorkerShutdownRequested(BaseException):
    def __init__(self, signum: int) -> None:
        super().__init__(f"container worker shutdown requested by signal {signum}")
        self.signum = signum


class ContainerWorkerRegistrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ContainerWorkerProcessResult:
    processed: bool
    worker_id: str
    status: str
    action: str
    container_id: str = ""
    error_message: str = ""

    @classmethod
    def from_scheduler_result(
        cls,
        result: WorkerSchedulerRequestResult,
    ) -> ContainerWorkerProcessResult:
        return cls(
            processed=result.processed,
            worker_id=result.worker_id,
            status=result.status.value,
            action=result.action.value,
            container_id=result.container_id,
            error_message=result.error_message or result.capacity_release_error,
        )

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "processed": self.processed,
            "worker_id": self.worker_id,
            "status": self.status,
            "action": self.action,
            "container_id": self.container_id,
            "error_message": self.error_message,
        }


@dataclass(slots=True)
class ContainerWorkerEventLoop:
    stop_event: threading.Event
    thread: threading.Thread

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.0)


@dataclass(slots=True)
class ContainerWorkerKeepaliveLoop:
    stop_event: threading.Event
    thread: threading.Thread

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join()


@dataclass(frozen=True, slots=True)
class WorkerArtifactRetentionLoop:
    stop_event: threading.Event
    thread: threading.Thread

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CONTAINER_WORKER_PROCESS_NAME)
    parser.add_argument("--worker-id")
    parser.add_argument("--pool", dest="pool_name", default=None)
    parser.add_argument("--machine-id")
    parser.add_argument("--pod-address")
    parser.add_argument("--container-service-port", type=int)
    parser.add_argument("--runtime", choices=[item.value for item in OciRuntimeName])
    parser.add_argument("--pool-mode", choices=[item.value for item in WorkerPoolMode])
    parser.add_argument("--worker-repository-url")
    parser.add_argument("--worker-token")
    parser.add_argument("--worker-repository-timeout-seconds", type=float)
    parser.add_argument("--interval-seconds", type=float, default=0.1)
    parser.add_argument(
        "--keepalive-interval-seconds",
        type=float,
        default=DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS,
    )
    parser.add_argument(
        "--heartbeat-file",
        type=Path,
        default=heartbeat_path(CONTAINER_WORKER_PROCESS_NAME),
        help="worker lifecycle progress heartbeat file",
    )
    parser.add_argument("--worker-spindown-seconds", type=float)
    parser.add_argument("--metrics-interval-seconds", type=float)
    parser.add_argument("--metrics-enabled", action="store_true", default=None)
    parser.add_argument("--no-metrics", action="store_false", dest="metrics_enabled")
    parser.add_argument("--container-cost-hook-endpoint")
    parser.add_argument("--container-cost-hook-token")
    parser.add_argument("--container-cost-hook-timeout-seconds", type=float)
    parser.add_argument("--gpu-devices")
    parser.add_argument(
        "--nvidia-cdi",
        action="store_true",
        dest="nvidia_cdi_enabled",
        default=None,
    )
    parser.add_argument("--no-nvidia-cdi", action="store_false", dest="nvidia_cdi_enabled")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--persistent", action="store_true", default=None)
    parser.add_argument("--agent-worker", action="store_true", default=None)
    parser.add_argument("--no-agent-worker", action="store_false", dest="agent_worker")
    parser.add_argument("--agent-bridge-network", action="store_true", default=None)
    parser.add_argument(
        "--no-agent-bridge-network",
        action="store_false",
        dest="agent_bridge_network",
    )
    parser.add_argument("--network-prefix")
    parser.add_argument("--route-transport")
    parser.add_argument("--route-target", dest="route_local_target_host")
    parser.add_argument("--bundle-root", type=Path)
    parser.add_argument("--image-cache-path")
    parser.add_argument("--image-mount-root")
    parser.add_argument("--image-archive-extension")
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--checkpoint-root")
    parser.add_argument("--checkpoint-bucket")
    parser.add_argument("--data-storage-mode", choices=[item.value for item in StorageMountMode])
    parser.add_argument("--data-storage-path")
    parser.add_argument("--data-storage-bucket")
    parser.add_argument("--data-storage-juicefs-redis-url")
    parser.add_argument(
        "--workspace-storage-mode",
        choices=[item.value for item in WorkerStorageMode],
    )
    parser.add_argument("--workspace-storage-base-mount-path")
    parser.add_argument("--workspace-storage-mountpoint-binary")
    return parser


def run_container_worker(
    *,
    settings: ProductionWorkerSettings,
    once: bool = False,
    interval_seconds: float = 0.1,
    keepalive_interval_seconds: float = DEFAULT_WORKER_KEEPALIVE_INTERVAL_SECONDS,
    heartbeat_file: Path | None = None,
    services: ContainerWorkerServices | None = None,
) -> ContainerWorkerProcessResult | None:
    _validate_keepalive_interval(keepalive_interval_seconds)
    if services is not None:
        runtime = ContainerWorkerRuntime.from_services(
            settings=settings,
            services=services,
        )
    else:
        runtime = ContainerWorkerRuntime.production(settings=settings)
    resolved_settings = runtime.settings
    worker_services = runtime.services
    container_service = _start_container_service(resolved_settings, worker_services)
    event_loop: ContainerWorkerEventLoop | None = None
    keepalive_loop: ContainerWorkerKeepaliveLoop | None = None
    artifact_retention_loop: WorkerArtifactRetentionLoop | None = None
    shutdown_event = threading.Event()
    shutdown_registered_worker = False
    shutdown_signal = 0

    def record_shutdown_signal(signum: int) -> None:
        nonlocal shutdown_signal
        shutdown_signal = signum

    try:
        try:
            with _container_worker_shutdown_handlers(
                shutdown_event,
                enabled=not once,
                on_signal=record_shutdown_signal,
            ):
                registration = worker_services.lifecycle.register_available()
                if not registration or not all(
                    step.status is WorkerLifecycleStatus.Ok for step in registration
                ):
                    shutdown_registered_worker = any(
                        step.status is WorkerLifecycleStatus.Ok for step in registration
                    )
                    for step in registration:
                        if step.status is not WorkerLifecycleStatus.Ok:
                            print(
                                "container worker registration failed: "
                                f"{step.action.value}: {step.error_message}",
                                file=sys.stderr,
                            )
                    detail = (
                        "; ".join(
                            f"{step.action.value}: {step.error_message}"
                            for step in registration
                            if step.status is not WorkerLifecycleStatus.Ok
                        )
                        or "worker registration returned no steps"
                    )
                    raise ContainerWorkerRegistrationError(detail)
                if once:
                    _reconcile_worker_artifacts(worker_services)
                    worker_services.lifecycle.keepalive()
                    _handle_worker_events_once(worker_services)
                    result = worker_services.processor.run_once()
                    _handle_worker_events_once(worker_services)
                    return ContainerWorkerProcessResult.from_scheduler_result(result)

                shutdown_registered_worker = True
                event_loop = _start_worker_event_loop(
                    worker_services,
                    interval_seconds=interval_seconds,
                    stop_event=shutdown_event,
                )
                artifact_retention_loop = _start_artifact_retention_loop(
                    worker_services,
                    interval_seconds=resolved_settings.artifact_retention_interval_seconds,
                    stop_event=shutdown_event,
                )
                heartbeat = None if heartbeat_file is None else HeartbeatFile(heartbeat_file)
                initial_keepalive = worker_services.lifecycle.keepalive()
                if heartbeat is not None:
                    heartbeat.beat()
                keepalive_loop = _start_worker_keepalive_loop(
                    worker_services,
                    interval_seconds=keepalive_interval_seconds,
                    stop_event=shutdown_event,
                    heartbeat=heartbeat,
                    consecutive_failures=_report_keepalive_result(
                        initial_keepalive,
                        consecutive_failures=0,
                    ),
                )
                last_request_at = time.monotonic()
                while not shutdown_event.is_set():
                    try:
                        now = time.monotonic()
                        result = worker_services.processor.run_once()
                        if result.processed:
                            last_request_at = now
                        spindown = worker_services.lifecycle.spindown_plan(
                            persistent=resolved_settings.resolved_persistent,
                            seconds_since_last_request=now - last_request_at,
                            spindown_seconds=resolved_settings.worker_spindown_seconds,
                        )
                        if spindown.should_shutdown:
                            break
                    except Exception as exc:
                        if shutdown_event.is_set():
                            break
                        print(
                            f"container worker loop error: {type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
                    shutdown_event.wait(interval_seconds)
        except ContainerWorkerShutdownRequested:
            return None
    finally:
        shutdown_event.set()
        if keepalive_loop is not None:
            keepalive_loop.stop()
        if artifact_retention_loop is not None:
            artifact_retention_loop.stop()
        if event_loop is not None:
            event_loop.stop()
        if shutdown_registered_worker:
            worker_services.lifecycle.shutdown(
                remove_worker=not resolved_settings.resolved_persistent,
                stop_reason=(
                    StopContainerReason.Preempted
                    if shutdown_signal == signal.SIGTERM
                    else StopContainerReason.Admin
                    if shutdown_signal == signal.SIGINT
                    else StopContainerReason.Unknown
                ),
            )
        if container_service is not None:
            container_service.stop()
        runtime.close()


def _reconcile_worker_artifacts(services: ContainerWorkerServices) -> None:
    retention = services.artifact_retention
    if retention is None:
        return
    result = retention.reconcile()
    if result.removed:
        print(
            "container worker artifact retention "
            f"removed={result.removed} freed_bytes={result.freed_bytes} "
            f"build_scratch_active={result.image_build_scratch_active}",
            file=sys.stderr,
        )
    for failure in result.image_build_scratch_cleanup_failures:
        print(
            f"container worker image build scratch cleanup failed: {failure}",
            file=sys.stderr,
        )


def _validate_keepalive_interval(interval_seconds: float) -> None:
    if interval_seconds <= 0 or interval_seconds > MAX_WORKER_KEEPALIVE_INTERVAL_SECONDS:
        msg = (
            "worker keepalive interval must be greater than zero and no more than "
            f"{MAX_WORKER_KEEPALIVE_INTERVAL_SECONDS:g} seconds"
        )
        raise ValueError(msg)


def _start_worker_keepalive_loop(
    services: ContainerWorkerServices,
    *,
    interval_seconds: float,
    stop_event: threading.Event,
    heartbeat: HeartbeatFile | None,
    consecutive_failures: int,
) -> ContainerWorkerKeepaliveLoop:
    thread = threading.Thread(
        target=_run_worker_keepalive_loop,
        args=(
            services,
            stop_event,
            interval_seconds,
            heartbeat,
            consecutive_failures,
        ),
        name="container-worker-keepalive",
        daemon=True,
    )
    thread.start()
    return ContainerWorkerKeepaliveLoop(stop_event=stop_event, thread=thread)


def _run_worker_keepalive_loop(
    services: ContainerWorkerServices,
    stop_event: threading.Event,
    interval_seconds: float,
    heartbeat: HeartbeatFile | None,
    consecutive_failures: int,
) -> None:
    failures = consecutive_failures
    while not stop_event.wait(interval_seconds):
        try:
            result = services.lifecycle.keepalive()
        except Exception as exc:
            result = WorkerLifecycleStepResult(
                action=WorkerLifecycleAction.KeepAlive,
                status=WorkerLifecycleStatus.Error,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        if heartbeat is not None:
            heartbeat.beat()
        failures = _report_keepalive_result(
            result,
            consecutive_failures=failures,
        )


def _report_keepalive_result(
    result: WorkerLifecycleStepResult,
    *,
    consecutive_failures: int,
) -> int:
    if result.ok:
        if consecutive_failures:
            print(
                "container worker keepalive recovered after "
                f"{consecutive_failures} consecutive failures",
                file=sys.stderr,
            )
        return 0
    failures = consecutive_failures + 1
    if failures == 1 or failures % 20 == 0:
        print(
            "container worker keepalive failed "
            f"consecutive_failures={failures}: {result.error_message}",
            file=sys.stderr,
        )
    return failures


def _start_artifact_retention_loop(
    services: ContainerWorkerServices,
    *,
    interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> WorkerArtifactRetentionLoop | None:
    if services.artifact_retention is None:
        return None
    resolved_stop_event = stop_event or threading.Event()
    thread = threading.Thread(
        target=_run_artifact_retention_loop,
        args=(services, resolved_stop_event, interval_seconds),
        name="container-worker-artifact-retention",
        daemon=True,
    )
    thread.start()
    return WorkerArtifactRetentionLoop(stop_event=resolved_stop_event, thread=thread)


def _run_artifact_retention_loop(
    services: ContainerWorkerServices,
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    consecutive_failures = 0
    while not stop_event.is_set():
        try:
            _reconcile_worker_artifacts(services)
        except Exception as exc:
            consecutive_failures += 1
            retry_seconds = min(
                15 * 60,
                30 * (1 << min(consecutive_failures - 1, 5)),
            )
            print(
                "container worker artifact retention error: "
                f"{type(exc).__name__}: {exc}; retry_seconds={retry_seconds}",
                file=sys.stderr,
            )
            stop_event.wait(retry_seconds)
            continue
        consecutive_failures = 0
        stop_event.wait(interval_seconds)


@contextmanager
def _container_worker_shutdown_handlers(
    shutdown_event: threading.Event,
    *,
    enabled: bool = True,
    on_signal: Callable[[int], None] | None = None,
):
    if not enabled or threading.current_thread() is not threading.main_thread():
        yield
        return
    signals = (signal.SIGTERM, signal.SIGINT)

    def request_shutdown(signum: int, frame: FrameType | None) -> None:
        if on_signal is not None:
            on_signal(signum)
        shutdown_event.set()
        _raise_container_worker_shutdown(signum, frame)

    previous_handlers = {signum: signal.signal(signum, request_shutdown) for signum in signals}
    try:
        yield
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def _raise_container_worker_shutdown(signum: int, _frame: FrameType | None) -> None:
    raise ContainerWorkerShutdownRequested(signum)


def main(argv: list[str] | None = None) -> None:
    args = _parse_arguments(argv)
    result = run_container_worker(
        once=args.once,
        interval_seconds=args.interval_seconds,
        keepalive_interval_seconds=args.keepalive_interval_seconds,
        heartbeat_file=args.heartbeat_file,
        settings=_settings_from_args(args),
    )
    if result is not None:
        print(json.dumps(result.to_dict(), sort_keys=True))


def _parse_arguments(argv: list[str] | None = None) -> ContainerWorkerArguments:
    arguments = ContainerWorkerArguments()
    build_parser().parse_args(argv, namespace=arguments)
    return arguments


def _settings_from_args(args: ContainerWorkerArguments) -> ProductionWorkerSettings:
    base = ProductionWorkerSettings()
    return ProductionWorkerSettings(
        configuration=base.configuration,
        worker_id=_override(args.worker_id, base.worker_id),
        pool_name=_override(args.pool_name, base.pool_name),
        machine_id=_override(args.machine_id, base.machine_id),
        pod_address=_override(args.pod_address, base.pod_address),
        container_service_port=_override(
            args.container_service_port,
            base.container_service_port,
        ),
        runtime=(OciRuntimeName(args.runtime) if args.runtime is not None else base.runtime),
        pool_mode=(
            WorkerPoolMode(args.pool_mode) if args.pool_mode is not None else base.pool_mode
        ),
        worker_repository_url=_override(
            args.worker_repository_url,
            base.worker_repository_url,
        ),
        worker_token=_override(args.worker_token, base.worker_token),
        worker_repository_timeout_seconds=_override(
            args.worker_repository_timeout_seconds,
            base.worker_repository_timeout_seconds,
        ),
        worker_spindown_seconds=_override(
            args.worker_spindown_seconds,
            base.worker_spindown_seconds,
        ),
        metrics_interval_seconds=(
            args.metrics_interval_seconds
            if args.metrics_interval_seconds is not None
            else base.metrics_interval_seconds
        ),
        metrics_enabled=(
            args.metrics_enabled if args.metrics_enabled is not None else base.metrics_enabled
        ),
        container_cost_hook_endpoint=_override(
            args.container_cost_hook_endpoint,
            base.container_cost_hook_endpoint,
        ),
        container_cost_hook_token=_override(
            args.container_cost_hook_token,
            base.container_cost_hook_token,
        ),
        container_cost_hook_timeout_seconds=_override(
            args.container_cost_hook_timeout_seconds,
            base.container_cost_hook_timeout_seconds,
        ),
        gpu_devices=_override(args.gpu_devices, base.gpu_devices),
        nvidia_cdi_enabled=_override(
            args.nvidia_cdi_enabled,
            base.nvidia_cdi_enabled,
        ),
        persistent=args.persistent if args.persistent is not None else base.persistent,
        agent_worker=args.agent_worker if args.agent_worker is not None else base.agent_worker,
        agent_bridge_network=(
            args.agent_bridge_network
            if args.agent_bridge_network is not None
            else base.agent_bridge_network
        ),
        network_prefix=(
            args.network_prefix if args.network_prefix is not None else base.network_prefix
        ),
        route_transport=(
            BackendRouteTransport(args.route_transport)
            if args.route_transport is not None
            else base.route_transport
        ),
        route_local_target_host=_override(
            args.route_local_target_host,
            base.route_local_target_host,
        ),
        bundle_root=args.bundle_root if args.bundle_root is not None else base.bundle_root,
        image_cache_path=(
            args.image_cache_path if args.image_cache_path is not None else base.image_cache_path
        ),
        image_mount_root=(
            args.image_mount_root if args.image_mount_root is not None else base.image_mount_root
        ),
        image_archive_extension=_override(
            args.image_archive_extension,
            base.image_archive_extension,
        ),
        cache_root=args.cache_root if args.cache_root is not None else base.cache_root,
        checkpoint_root=(
            args.checkpoint_root if args.checkpoint_root is not None else base.checkpoint_root
        ),
        checkpoint_bucket=_override(args.checkpoint_bucket, base.checkpoint_bucket),
        data_storage_mode=(
            StorageMountMode(args.data_storage_mode)
            if args.data_storage_mode is not None
            else base.data_storage_mode
        ),
        data_storage_path=(
            args.data_storage_path if args.data_storage_path is not None else base.data_storage_path
        ),
        data_storage_bucket=_override(args.data_storage_bucket, base.data_storage_bucket),
        data_storage_juicefs_redis_url=_override(
            args.data_storage_juicefs_redis_url,
            base.data_storage_juicefs_redis_url,
        ),
        workspace_storage_mode=(
            WorkerStorageMode(args.workspace_storage_mode)
            if args.workspace_storage_mode is not None
            else base.workspace_storage_mode
        ),
        workspace_storage_base_mount_path=_override(
            args.workspace_storage_base_mount_path,
            base.workspace_storage_base_mount_path,
        ),
        workspace_storage_mountpoint_binary=_override(
            args.workspace_storage_mountpoint_binary,
            base.workspace_storage_mountpoint_binary,
        ),
    )


def _override[T](value: T | None, default: T) -> T:
    return default if value is None else value


def _start_container_service(
    settings: ProductionWorkerSettings,
    services: ContainerWorkerServices,
) -> ContainerServiceHttpServer | None:
    if settings.container_service_port <= 0:
        return None
    container_transport = _container_transport(services)
    if container_transport is None:
        msg = "container service transport is required when container service port is enabled"
        raise ValueError(msg)
    return start_container_service_http_server(
        container_transport,
        host="0.0.0.0",
        port=settings.container_service_port,
        token=settings.container_service_token,
    )


def _start_worker_event_loop(
    services: ContainerWorkerServices,
    *,
    interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> ContainerWorkerEventLoop | None:
    if _worker_event_source(services) is None or _worker_event_handler(services) is None:
        return None
    resolved_stop_event = stop_event or threading.Event()
    thread = threading.Thread(
        target=_run_worker_event_loop,
        args=(
            services,
            resolved_stop_event,
            max(interval_seconds, WORKER_EVENT_POLL_INTERVAL_SECONDS),
        ),
        name="container-worker-events",
        daemon=True,
    )
    thread.start()
    return ContainerWorkerEventLoop(stop_event=resolved_stop_event, thread=thread)


def _run_worker_event_loop(
    services: ContainerWorkerServices,
    stop_event: threading.Event,
    interval_seconds: float,
) -> None:
    while not stop_event.is_set():
        try:
            _handle_worker_events(services, max_events=0, stop_event=stop_event)
        except Exception as exc:
            print(
                f"container worker event loop error: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        stop_event.wait(interval_seconds)


def _handle_worker_events_once(services: ContainerWorkerServices) -> int:
    return _handle_worker_events(services, max_events=WORKER_EVENT_BATCH_SIZE)


def _handle_worker_events(
    services: ContainerWorkerServices,
    *,
    max_events: int,
    stop_event: threading.Event | None = None,
) -> int:
    event_source = _worker_event_source(services)
    event_handler = _worker_event_handler(services)
    if event_source is None or event_handler is None:
        return 0
    handled = 0
    request = StreamWorkerEventsRequest(
        worker_id=_worker_id(services),
        max_events=max_events,
        heartbeat_interval_seconds=WORKER_EVENT_HEARTBEAT_INTERVAL_SECONDS,
        pubsub_timeout_seconds=WORKER_EVENT_PUBSUB_TIMEOUT_SECONDS,
    )
    for event in event_source.stream_worker_events(request):
        if stop_event is not None and stop_event.is_set():
            break
        if event.kind == WorkerStreamEventKind.Heartbeat:
            continue
        result = event_handler.handle(event)
        handled += 1
        if not result.ok:
            print(
                "container worker event handling error: "
                f"{result.status.value}: {result.error_message or result.reason}",
                file=sys.stderr,
            )
    return handled


def _worker_event_source(
    services: ContainerWorkerServices,
) -> ContainerWorkerEventSource | None:
    return getattr(services, "event_source", None)


def _worker_event_handler(
    services: ContainerWorkerServices,
) -> ContainerWorkerEventHandler | None:
    return getattr(services, "worker_events", None)


def _container_transport(
    services: ContainerWorkerServices,
) -> ContainerServiceRequestHandler | None:
    if isinstance(services, ContainerWorkerTransportServices):
        return services.container_transport
    return None


def _worker_id(services: ContainerWorkerServices) -> str:
    identity = getattr(services, "identity", None)
    worker_id = getattr(identity, "worker_id", "")
    return worker_id if isinstance(worker_id, str) else ""


if __name__ == "__main__":
    main()

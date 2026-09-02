from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import Field, JsonValue, SecretStr, TypeAdapter, model_validator
from shared.containers import ContainerStatus
from shared.contracts import ContractModel
from shared.http.compute import ContainerDetailResponse
from shared.http.observability import LogRecord
from worker.events import WORKER_EVENT_HEARTBEAT_ID, ContainerEventPayload, WorkerStreamEvent
from worker.repository_payloads import (
    AcknowledgeContainerRequestRequest,
    AcknowledgeContainerRequestResponse,
    AppendContainerLogsRequest,
    AppendContainerLogsResponse,
    ContainerLogBatchEntry,
    ContainerLogStream,
    GetNextContainerRequestRequest,
    GetNextContainerRequestResponse,
    PublishContainerEventRequest,
    PublishContainerEventResponse,
    StreamWorkerEventsRequest,
    WorkerCacheSessionRequest,
    WorkerKeepAliveResponse,
)

DEFAULT_CUSTOMER_STREAMS = 500
DEFAULT_WORKER_STREAMS = 1_000
DEFAULT_STEADY_SECONDS = 10.0
DEFAULT_RECOVERY_SECONDS = 45.0
DEFAULT_CONNECT_SECONDS = 60.0
DEFAULT_RESTART_TIMEOUT_SECONDS = 60.0
# A stream that parked its own XREAD would show as one blocked client per
# stream, so blocked clients get a small fixed allowance. Connected clients
# include the async pool's high-water mark from the connection burst, and that
# pool never shrinks, so the connected bound only rules out one connection per
# customer stream.
REDIS_BLOCKED_CLIENT_GROWTH_LIMIT = 8
REDIS_CLIENT_GROWTH_PER_CUSTOMER_STREAM = 0.5

_BACKOFF_MAX_SECONDS = 2.0
_POLL_SECONDS = 0.5
_POOL_EXHAUSTION_MARKERS = (
    "connections are exhausted",
    "max connections",
    "no connection available",
    "pool exhaustion",
    "pool timeout",
    "too many connections",
)
_RUN_ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class _StreamKind(StrEnum):
    CustomerEvents = "customer-event"
    CustomerLogs = "customer-log"
    WorkerRequests = "worker-request"
    WorkerEvents = "worker-event"

    @property
    def customer(self) -> bool:
        return self in {_StreamKind.CustomerEvents, _StreamKind.CustomerLogs}


class _RequestFailed(RuntimeError):
    pass


class CustomerStreamTarget(ContractModel):
    token: SecretStr
    workspace: str
    container_id: str
    event_payload: ContainerEventPayload

    @model_validator(mode="after")
    def marker_identity_matches_stream(self) -> CustomerStreamTarget:
        if not self.workspace.strip() or not self.container_id.strip():
            raise ValueError("customer stream workspace and container id are required")
        if self.event_payload.workspace_id != self.workspace:
            raise ValueError("customer event payload workspace must match the stream workspace")
        if self.event_payload.container_id != self.container_id:
            raise ValueError("customer event payload container must match the streamed container")
        return self


class DurableContainerExpectation(ContractModel):
    container_id: str
    workspace: str
    allowed_statuses: tuple[ContainerStatus, ...]

    @model_validator(mode="after")
    def durable_contract_is_specific(self) -> DurableContainerExpectation:
        if not self.container_id.strip() or not self.workspace.strip():
            raise ValueError("durable container identity is required")
        if not self.allowed_statuses:
            raise ValueError("durable container status contract cannot be empty")
        if len(set(self.allowed_statuses)) != len(self.allowed_statuses):
            raise ValueError("durable container statuses must be unique")
        return self


class WorkerStreamIdentity(ContractModel):
    token: SecretStr
    request: GetNextContainerRequestRequest

    @model_validator(mode="after")
    def identity_is_benchmark_owned(self) -> WorkerStreamIdentity:
        if not self.request.worker_id.startswith("benchmark-"):
            raise ValueError("live benchmark worker ids must start with 'benchmark-'")
        return self


class DeliveryWorkerStreamIdentity(WorkerStreamIdentity):
    expected_requests: tuple[DurableContainerExpectation, ...] = Field(min_length=1)


class ControlPlaneStreamManifest(ContractModel):
    customer: CustomerStreamTarget
    load_worker: WorkerStreamIdentity
    delivery_worker: DeliveryWorkerStreamIdentity

    @model_validator(mode="after")
    def benchmark_ownership_is_unambiguous(self) -> ControlPlaneStreamManifest:
        if self.load_worker.request.worker_id == self.delivery_worker.request.worker_id:
            raise ValueError("load and delivery worker ids must be distinct")
        expected_requests = [item.container_id for item in self.delivery_worker.expected_requests]
        if len(set(expected_requests)) != len(expected_requests):
            raise ValueError("expected container request ids must be unique")
        if any(
            item.workspace != self.customer.workspace
            for item in self.delivery_worker.expected_requests
        ):
            raise ValueError("durable request verification must use the customer workspace")
        if self.customer.container_id not in expected_requests:
            raise ValueError("customer stream container must be a delivery-worker request")
        if self.customer.event_payload.worker_id != self.delivery_worker.request.worker_id:
            raise ValueError("customer marker publisher must be the delivery worker")
        return self


class ControlPlaneStreamReport(ContractModel):
    run_id: str
    endpoint: str
    customer_streams: int
    customer_event_streams: int
    customer_log_streams: int
    worker_streams: int
    worker_request_streams: int
    worker_event_streams: int
    load_worker_streams: int
    delivery_worker_streams: int
    steady_seconds: float
    connect_seconds: float
    recovery_seconds: float
    restart_timeout_seconds: float
    expected_requests: int
    acknowledged_requests: int
    durably_verified_requests_before_restart: int
    durably_verified_requests_after_restart: int
    redelivered_acknowledged_requests: int
    initial_ready_customer_streams: int
    initial_ready_worker_streams: int
    recovered_customer_streams: int
    recovered_worker_streams: int
    customer_cursor_redeliveries: int
    connection_attempts: int
    reconnects_after_restart: int
    transient_disconnects: int
    pool_exhaustion_errors: int
    redis_clients_sampled: bool = False
    redis_clients_baseline: int = 0
    redis_clients_peak: int = 0
    redis_blocked_clients_baseline: int = 0
    redis_blocked_clients_peak: int = 0
    request_errors: tuple[str, ...] = ()
    restart_completed: bool = False
    duration_seconds: float = Field(ge=0)
    failure: str = ""
    passed: bool

    def to_json(self) -> str:
        return self.model_dump_json(indent=2) + "\n"

    def to_markdown(self) -> str:
        status = "passed" if self.passed else "failed"
        lines = [
            f"# Control-plane stream benchmark: {status}",
            "",
            f"- Run: {self.run_id}",
            (
                f"- Customer streams: {self.recovered_customer_streams}/"
                f"{self.customer_streams} ({self.customer_event_streams} event, "
                f"{self.customer_log_streams} log)"
            ),
            (
                f"- Worker streams: {self.recovered_worker_streams}/{self.worker_streams} "
                f"({self.worker_request_streams} request, {self.worker_event_streams} event)"
            ),
            (
                f"- Worker identities: {self.load_worker_streams} load streams, "
                f"{self.delivery_worker_streams} delivery stream"
            ),
            (
                "- Requests acknowledged and durably verified: "
                f"{self.acknowledged_requests}/{self.expected_requests}; post-restart "
                f"{self.durably_verified_requests_after_restart}"
            ),
            f"- Acknowledged request redeliveries: {self.redelivered_acknowledged_requests}",
            f"- Cursor redeliveries: {self.customer_cursor_redeliveries}",
            f"- Reconnects after restart: {self.reconnects_after_restart}",
            f"- Pool exhaustion errors: {self.pool_exhaustion_errors}",
            f"- Transient disconnects: {self.transient_disconnects}",
            f"- Duration: {self.duration_seconds:.2f}s",
        ]
        if self.redis_clients_sampled:
            lines.append(
                "- Redis clients during steady phases: "
                f"{self.redis_clients_peak} peak over {self.redis_clients_baseline} baseline; "
                f"blocked {self.redis_blocked_clients_peak} peak over "
                f"{self.redis_blocked_clients_baseline} baseline"
            )
        if self.failure:
            lines.append(f"- Failure: {self.failure}")
        lines.append("")
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ControlPlaneStreamConfig:
    endpoint: str
    manifest: Path | None
    restart_command: tuple[str, ...]
    redis_info_command: tuple[str, ...] = ()
    customer_streams: int = DEFAULT_CUSTOMER_STREAMS
    worker_streams: int = DEFAULT_WORKER_STREAMS
    steady_seconds: float = DEFAULT_STEADY_SECONDS
    connect_seconds: float = DEFAULT_CONNECT_SECONDS
    recovery_seconds: float = DEFAULT_RECOVERY_SECONDS
    restart_timeout_seconds: float = DEFAULT_RESTART_TIMEOUT_SECONDS
    run_id: str = ""

    def __post_init__(self) -> None:
        if self.customer_streams < 2 or self.worker_streams < 2:
            raise ValueError("stream counts must be at least two to cover both stream owners")
        if (
            min(
                self.steady_seconds,
                self.connect_seconds,
                self.recovery_seconds,
                self.restart_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("benchmark timing values must be positive")
        if not self.restart_command:
            raise ValueError("a replica restart command is required")
        parsed = urlsplit(self.endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "benchmark endpoint must be an http(s) origin without credentials, query, "
                "or fragment"
            )
        run_id = self.run_id or uuid4().hex
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ValueError("run id must be 1-64 URL-safe identifier characters")
        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(self, "endpoint", self.endpoint.rstrip("/"))


@dataclass(frozen=True, slots=True)
class _RedisClientSample:
    connected: int
    blocked: int

    def peak(self, other: _RedisClientSample) -> _RedisClientSample:
        return _RedisClientSample(
            connected=max(self.connected, other.connected),
            blocked=max(self.blocked, other.blocked),
        )


@dataclass(slots=True)
class _StreamState:
    kind: _StreamKind
    name: str
    connected: bool = False
    validated_at: float = 0
    connection_attempts: int = 0
    attempts_at_restart: int = 0
    disconnects: int = 0
    consecutive_failures: int = 0
    cursor: str = ""
    marker_phase: int = 0
    seen_markers: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _WorkerStreamAssignment:
    kind: _StreamKind
    identity: WorkerStreamIdentity


@dataclass(slots=True)
class _RunState:
    streams: list[_StreamState]
    acknowledged_requests: set[str] = field(default_factory=set)
    verified_before_restart: set[str] = field(default_factory=set)
    verified_after_restart: set[str] = field(default_factory=set)
    redelivered_requests: set[str] = field(default_factory=set)
    request_errors: list[str] = field(default_factory=list)
    pool_exhaustion_errors: int = 0
    cursor_redeliveries: int = 0

    def record_error(self, message: str) -> str:
        normalized = message.strip().replace("\n", " ")[:300]
        if normalized and normalized not in self.request_errors:
            self.request_errors.append(normalized)
        return normalized

    def record_http_error(self, context: str, status_code: int, body: bytes) -> str:
        body_text = body.decode(errors="replace").lower()
        message = f"{context} returned HTTP {status_code}"
        if any(marker in body_text for marker in _POOL_EXHAUSTION_MARKERS):
            self.pool_exhaustion_errors += 1
            message += " (database or Redis pool exhaustion)"
        return self.record_error(message)

    def counts(self, *, restarted_at: float = 0) -> dict[str, int]:
        customer = [state for state in self.streams if state.kind.customer]
        workers = [state for state in self.streams if not state.kind.customer]
        return {
            "customer_ready": sum(state.marker_phase >= 1 for state in customer),
            "worker_ready": sum(state.validated_at > 0 for state in workers),
            "customer_recovered": sum(state.marker_phase >= 2 for state in customer),
            "worker_recovered": sum(state.validated_at >= restarted_at for state in workers),
            "connected": sum(state.connected for state in self.streams),
            "attempts": sum(state.connection_attempts for state in self.streams),
            "reconnects": sum(
                state.connection_attempts > state.attempts_at_restart for state in self.streams
            ),
            "disconnects": sum(state.disconnects for state in self.streams),
            "acknowledged_requests": len(self.acknowledged_requests),
            "errors": len(self.request_errors),
            "pool_exhaustion": self.pool_exhaustion_errors,
        }


def emit_progress(phase: str, **values: object) -> None:
    print(json.dumps({"phase": phase, **values}, sort_keys=True), file=sys.stderr, flush=True)


def _bearer(token: SecretStr) -> dict[str, str]:
    return {"Authorization": f"Bearer {token.get_secret_value()}"}


class _ControlPlaneStreamRun:
    def __init__(
        self,
        config: ControlPlaneStreamConfig,
        manifest: ControlPlaneStreamManifest,
    ) -> None:
        self.config = config
        self.manifest = manifest
        self.stop = asyncio.Event()
        self.customer_event_count = config.customer_streams // 2
        self.customer_log_count = config.customer_streams - self.customer_event_count
        self.worker_request_count = 1
        self.worker_event_count = config.worker_streams - self.worker_request_count
        self.worker_assignments = [
            _WorkerStreamAssignment(
                kind=_StreamKind.WorkerRequests,
                identity=manifest.delivery_worker,
            ),
            *(
                _WorkerStreamAssignment(
                    kind=_StreamKind.WorkerEvents,
                    identity=manifest.load_worker,
                )
                for _ in range(self.worker_event_count)
            ),
        ]
        customer_states = [
            *(
                _StreamState(kind=_StreamKind.CustomerEvents, name=f"customer-event-{index}")
                for index in range(self.customer_event_count)
            ),
            *(
                _StreamState(kind=_StreamKind.CustomerLogs, name=f"customer-log-{index}")
                for index in range(self.customer_log_count)
            ),
        ]
        worker_states = [
            _StreamState(kind=assignment.kind, name=f"worker-stream-{index}")
            for index, assignment in enumerate(self.worker_assignments)
        ]
        self.state = _RunState(streams=[*customer_states, *worker_states])
        self._tasks: list[asyncio.Task[None]] = []
        self._event_markers: dict[int, str] = {}
        self._log_markers: dict[int, str] = {}
        self._restart_completed = False
        self._restarted_at = 0.0
        self._redis_baseline: _RedisClientSample | None = None
        self._redis_peak: _RedisClientSample | None = None
        self.client = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(connect=10, read=None, write=10, pool=10),
            limits=httpx.Limits(
                max_connections=config.customer_streams + config.worker_streams + 100,
                max_keepalive_connections=100,
            ),
        )

    async def run(self) -> ControlPlaneStreamReport:
        started = time.monotonic()
        initial_customer = initial_worker = recovered_customer = recovered_worker = 0
        failure = ""
        try:
            if self.config.redis_info_command:
                self._redis_baseline = await self._sample_redis_clients()
            self._tasks = self._start_streams()
            await self._poll(
                phase="initial-connections",
                timeout_seconds=self.config.connect_seconds,
                complete=lambda counts: (
                    counts["connected"] == len(self.state.streams)
                    and counts["worker_ready"] == self.config.worker_streams
                ),
            )
            await self._publish_customer_markers(phase=1)
            await self._poll(
                phase="initial-cursors-and-deliveries",
                timeout_seconds=self.config.connect_seconds,
                complete=lambda counts: (
                    counts["customer_ready"] == self.config.customer_streams
                    and self._all_expected_deliveries_acknowledged()
                ),
            )
            await self._verify_durable_contract(before_restart=True)
            initial = self.state.counts()
            initial_customer = initial["customer_ready"]
            initial_worker = initial["worker_ready"]
            await self._sustain("before-restart", self.config.steady_seconds)

            for stream in self.state.streams:
                stream.attempts_at_restart = stream.connection_attempts
            await self._restart_replica()
            self._restarted_at = time.monotonic()
            await self._poll(
                phase="restart-reconnections",
                timeout_seconds=self.config.recovery_seconds,
                complete=lambda counts: (
                    self._restart_was_observed()
                    and counts["connected"] == len(self.state.streams)
                    and counts["worker_recovered"] == self.config.worker_streams
                ),
                restarted_at=self._restarted_at,
            )
            await self._publish_customer_markers(phase=2)
            await self._poll(
                phase="restart-cursor-recovery",
                timeout_seconds=self.config.recovery_seconds,
                complete=lambda counts: (
                    counts["customer_recovered"] == self.config.customer_streams
                    and counts["worker_recovered"] == self.config.worker_streams
                    and self._restart_was_observed()
                ),
                restarted_at=self._restarted_at,
            )
            await self._verify_durable_contract(before_restart=False)
            recovered = self.state.counts(restarted_at=self._restarted_at)
            recovered_customer = recovered["customer_recovered"]
            recovered_worker = recovered["worker_recovered"]
            await self._sustain("after-restart", self.config.steady_seconds)
        except Exception as exc:
            failure = self._safe_failure(exc)
        finally:
            self.stop.set()
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            await self.client.aclose()

        return self._report(
            started=started,
            initial_customer=initial_customer,
            initial_worker=initial_worker,
            recovered_customer=recovered_customer,
            recovered_worker=recovered_worker,
            failure=failure,
        )

    def _report(
        self,
        *,
        started: float,
        initial_customer: int,
        initial_worker: int,
        recovered_customer: int,
        recovered_worker: int,
        failure: str,
    ) -> ControlPlaneStreamReport:
        counts = self.state.counts(restarted_at=self._restarted_at)
        expected_requests = set(self._expected_requests())
        passed = (
            not failure
            and initial_customer == self.config.customer_streams
            and initial_worker == self.config.worker_streams
            and recovered_customer == self.config.customer_streams
            and recovered_worker == self.config.worker_streams
            and self.state.acknowledged_requests == expected_requests
            and self.state.verified_before_restart == expected_requests
            and self.state.verified_after_restart == expected_requests
            and not self.state.redelivered_requests
            and self.state.cursor_redeliveries == 0
            and self.state.pool_exhaustion_errors == 0
            and not self.state.request_errors
            and self._restart_completed
            and self._redis_clients_within_limits()
        )
        baseline = self._redis_baseline or _RedisClientSample(connected=0, blocked=0)
        peak = self._redis_peak or baseline
        return ControlPlaneStreamReport(
            run_id=self.config.run_id,
            endpoint=self.config.endpoint,
            customer_streams=self.config.customer_streams,
            customer_event_streams=self.customer_event_count,
            customer_log_streams=self.customer_log_count,
            worker_streams=self.config.worker_streams,
            worker_request_streams=self.worker_request_count,
            worker_event_streams=self.worker_event_count,
            load_worker_streams=self.worker_event_count,
            delivery_worker_streams=self.worker_request_count,
            steady_seconds=self.config.steady_seconds,
            connect_seconds=self.config.connect_seconds,
            recovery_seconds=self.config.recovery_seconds,
            restart_timeout_seconds=self.config.restart_timeout_seconds,
            expected_requests=len(expected_requests),
            acknowledged_requests=len(self.state.acknowledged_requests),
            durably_verified_requests_before_restart=len(self.state.verified_before_restart),
            durably_verified_requests_after_restart=len(self.state.verified_after_restart),
            redelivered_acknowledged_requests=len(self.state.redelivered_requests),
            initial_ready_customer_streams=initial_customer,
            initial_ready_worker_streams=initial_worker,
            recovered_customer_streams=recovered_customer,
            recovered_worker_streams=recovered_worker,
            customer_cursor_redeliveries=self.state.cursor_redeliveries,
            connection_attempts=counts["attempts"],
            reconnects_after_restart=counts["reconnects"],
            transient_disconnects=counts["disconnects"],
            pool_exhaustion_errors=self.state.pool_exhaustion_errors,
            redis_clients_sampled=self._redis_baseline is not None,
            redis_clients_baseline=baseline.connected,
            redis_clients_peak=peak.connected,
            redis_blocked_clients_baseline=baseline.blocked,
            redis_blocked_clients_peak=peak.blocked,
            request_errors=tuple(self.state.request_errors),
            restart_completed=self._restart_completed,
            duration_seconds=time.monotonic() - started,
            failure=failure,
            passed=passed,
        )

    def _start_streams(self) -> list[asyncio.Task[None]]:
        tasks = [
            asyncio.create_task(self._customer_stream(state), name=f"benchmark-customer-{index}")
            for index, state in enumerate(self.state.streams[: self.config.customer_streams])
        ]
        offset = self.config.customer_streams
        tasks.extend(
            asyncio.create_task(
                self._worker_stream(self.state.streams[offset + index], assignment),
                name=f"benchmark-worker-{index}",
            )
            for index, assignment in enumerate(self.worker_assignments)
        )
        tasks.extend(
            asyncio.create_task(
                self._worker_keepalive(identity),
                name=f"benchmark-worker-keepalive-{index}",
            )
            for index, identity in enumerate(
                (self.manifest.load_worker, self.manifest.delivery_worker)
            )
        )
        return tasks

    async def _post_json(
        self,
        context: str,
        path: str,
        *,
        token: SecretStr,
        payload: ContractModel,
    ) -> JsonValue:
        response = await self.client.post(
            path, headers=_bearer(token), json=payload.model_dump(mode="json")
        )
        if response.status_code != 200:
            raise _RequestFailed(
                self.state.record_http_error(context, response.status_code, response.content)
            )
        return response.json()

    async def _worker_keepalive(self, identity: WorkerStreamIdentity) -> None:
        request = WorkerCacheSessionRequest(
            worker_id=identity.request.worker_id,
            cache_generation_id=identity.request.cache_generation_id,
            cache_session_fence=identity.request.cache_session_fence,
        )
        while not self.stop.is_set():
            try:
                body = await self._post_json(
                    "worker keepalive",
                    "/worker-repository/set-worker-keep-alive",
                    token=identity.token,
                    payload=request,
                )
                if WorkerKeepAliveResponse.model_validate(body).worker is None:
                    raise _RequestFailed(
                        self.state.record_error("worker keepalive returned no worker")
                    )
            except (httpx.HTTPError, OSError):
                pass
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=10)
            except TimeoutError:
                continue

    async def _customer_stream(self, state: _StreamState) -> None:
        target = self.manifest.customer
        if state.kind is _StreamKind.CustomerEvents:
            path = f"/api/v1/events/containers/{target.container_id}/stream"
            params = {"workspace": target.workspace, "follow": "true"}
        else:
            path = "/api/v1/logs/stream"
            params = {
                "workspace": target.workspace,
                "container_id": target.container_id,
                "follow": "true",
                "wait_seconds": "1",
            }
        while not self.stop.is_set():
            headers = _bearer(target.token)
            if state.cursor:
                headers["Last-Event-ID"] = state.cursor
            async with aclosing(
                self._consume_sse(state, method="GET", path=path, headers=headers, params=params)
            ) as events:
                async for _event, event_id, data in events:
                    self._handle_customer_event(state, event_id=event_id, data=data)
            await self._backoff(state)

    async def _worker_stream(
        self,
        state: _StreamState,
        assignment: _WorkerStreamAssignment,
    ) -> None:
        identity = assignment.identity
        headers = _bearer(identity.token)
        if assignment.kind is _StreamKind.WorkerRequests:
            path = "/worker-repository/get-next-container-request"
            payload: ContractModel = identity.request
        else:
            path = "/worker-repository/stream-worker-events"
            payload = StreamWorkerEventsRequest(
                worker_id=identity.request.worker_id,
                heartbeat_interval_seconds=1,
            )
        while not self.stop.is_set():
            async with aclosing(
                self._consume_sse(state, method="POST", path=path, headers=headers, payload=payload)
            ) as events:
                async for event, _event_id, data in events:
                    if assignment.kind is _StreamKind.WorkerRequests:
                        await self._handle_container_request(
                            state, event=event, data=data, identity=identity
                        )
                    else:
                        self._handle_worker_event(state, event=event, data=data)
            await self._backoff(state)

    async def _consume_sse(
        self,
        state: _StreamState,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        payload: ContractModel | None = None,
    ) -> AsyncGenerator[tuple[str, str, str], None]:
        state.connection_attempts += 1
        event = event_id = ""
        data_lines: list[str] = []
        connected = False
        body = None if payload is None else payload.model_dump(mode="json")
        try:
            async with self.client.stream(
                method, path, headers=headers, params=params, json=body
            ) as response:
                if response.status_code != 200:
                    self.state.record_http_error(
                        f"{state.kind} stream",
                        response.status_code,
                        await response.aread(),
                    )
                    return
                connected = state.connected = True
                state.consecutive_failures = 0
                async for raw_line in response.aiter_lines():
                    line = raw_line.rstrip("\r\n")
                    if line.startswith(":"):
                        continue
                    if not line:
                        if data_lines:
                            if event_id:
                                state.cursor = event_id
                            yield event, event_id, "\n".join(data_lines)
                        event = event_id = ""
                        data_lines = []
                        continue
                    field_name, separator, value = line.partition(":")
                    if not separator:
                        continue
                    value = value.removeprefix(" ")
                    if field_name == "event":
                        event = value
                    elif field_name == "id":
                        event_id = value
                    elif field_name == "data":
                        data_lines.append(value)
        except (httpx.HTTPError, OSError):
            state.consecutive_failures += 1
        finally:
            state.connected = False
            if connected and not self.stop.is_set():
                state.disconnects += 1

    def _handle_customer_event(self, state: _StreamState, *, event_id: str, data: str) -> None:
        if state.kind is _StreamKind.CustomerEvents:
            payload = _JSON_OBJECT.validate_json(data).get("data")
            marker_value = payload.get("id") if isinstance(payload, dict) else None
            marker = marker_value if isinstance(marker_value, str) else ""
            marker_map = self._event_markers
        else:
            marker = LogRecord.model_validate_json(data).message
            marker_map = self._log_markers
        phase = next((phase for phase, expected in marker_map.items() if expected == marker), 0)
        if not phase:
            return
        if not event_id:
            raise ValueError("benchmark marker arrived without a resumable SSE cursor")
        if marker in state.seen_markers:
            self.state.cursor_redeliveries += 1
            return
        state.seen_markers.add(marker)
        state.marker_phase = max(state.marker_phase, phase)

    async def _handle_container_request(
        self,
        state: _StreamState,
        *,
        event: str,
        data: str,
        identity: WorkerStreamIdentity,
    ) -> None:
        if event != "container-request":
            return
        response = GetNextContainerRequestResponse.model_validate_json(data)
        state.validated_at = time.monotonic()
        request = response.container_request
        if request is None:
            return
        expected = self._expected_requests().get(request.container_id)
        if expected is None:
            raise RuntimeError(
                "worker request stream received work not declared by the benchmark manifest"
            )
        if request.workspace_id != expected.workspace:
            raise RuntimeError("benchmark request workspace did not match its manifest")
        if request.container_id in self.state.acknowledged_requests:
            self.state.redelivered_requests.add(request.container_id)
            return
        body = await self._post_json(
            "container request acknowledgement",
            "/worker-repository/acknowledge-container-request",
            token=identity.token,
            payload=AcknowledgeContainerRequestRequest(
                worker_id=identity.request.worker_id,
                container_id=request.container_id,
            ),
        )
        if not AcknowledgeContainerRequestResponse.model_validate(body).acknowledged:
            raise _RequestFailed(
                self.state.record_error("benchmark container request was not acknowledged")
            )
        self.state.acknowledged_requests.add(request.container_id)

    @staticmethod
    def _handle_worker_event(state: _StreamState, *, event: str, data: str) -> None:
        if event != "worker-event":
            return
        stream_event = WorkerStreamEvent.model_validate_json(data)
        state.validated_at = time.monotonic()
        if stream_event.event_id == WORKER_EVENT_HEARTBEAT_ID:
            return
        raise RuntimeError(
            "benchmark worker event stream received actionable work; it was not acknowledged"
        )

    async def _publish_customer_markers(self, *, phase: int) -> None:
        target = self.manifest.customer
        phase_name = "before-restart" if phase == 1 else "after-restart"
        event_marker = f"benchmark:{self.config.run_id}:{phase_name}:event"
        log_marker = f"benchmark:{self.config.run_id}:{phase_name}:log"
        self._event_markers[phase] = event_marker
        self._log_markers[phase] = log_marker
        publisher = self.manifest.delivery_worker.token
        payload = target.event_payload.model_copy(
            update={
                "id": event_marker,
                "message": event_marker,
                "attrs": target.event_payload.attrs
                | {"benchmark_run_id": self.config.run_id, "benchmark_phase": phase_name},
            }
        )
        published = await self._post_json(
            "customer event marker publish",
            "/worker-repository/publish-container-event",
            token=publisher,
            payload=PublishContainerEventRequest(payload=payload),
        )
        if PublishContainerEventResponse.model_validate(published).event is None:
            raise RuntimeError("customer event marker publish returned no event")
        appended = await self._post_json(
            "customer log marker publish",
            "/worker-repository/append-container-logs",
            token=publisher,
            payload=AppendContainerLogsRequest(
                container_id=target.container_id,
                capture_id=f"benchmark-{self.config.run_id}-{phase_name}",
                entries=[
                    ContainerLogBatchEntry(
                        sequence=0,
                        stream=ContainerLogStream.Stdout,
                        message=log_marker,
                        timestamp=datetime.now(UTC),
                    )
                ],
            ),
        )
        result = AppendContainerLogsResponse.model_validate(appended)
        if result.appended_count != 1 or result.accepted_through != 0:
            raise RuntimeError("customer log marker was not appended exactly once")

    async def _verify_durable_contract(self, *, before_restart: bool) -> None:
        expected = self._expected_requests()
        verified: set[str] = set()
        verification_slots = asyncio.Semaphore(8)
        deadline = time.monotonic() + self.config.recovery_seconds
        while True:
            checks = await asyncio.gather(
                *(
                    self._verify_container(item, verification_slots)
                    for container_id, item in expected.items()
                    if container_id not in verified
                )
            )
            last_errors = [error for _container_id, error in checks if error]
            verified.update(container_id for container_id, error in checks if not error)
            emit_progress(
                "durable-before-restart" if before_restart else "durable-after-restart",
                expected=len(expected),
                verified=len(verified),
            )
            if len(verified) == len(expected):
                destination = (
                    self.state.verified_before_restart
                    if before_restart
                    else self.state.verified_after_restart
                )
                destination.update(verified)
                return
            if time.monotonic() >= deadline:
                detail = last_errors[0] if last_errors else "durable state did not match"
                raise TimeoutError(
                    f"durable request verification failed for "
                    f"{len(expected) - len(verified)} request(s): {detail}"
                )
            await asyncio.sleep(_POLL_SECONDS)

    async def _verify_container(
        self,
        expectation: DurableContainerExpectation,
        verification_slots: asyncio.Semaphore,
    ) -> tuple[str, str]:
        try:
            async with verification_slots:
                response = await self.client.get(
                    f"/api/v1/containers/{expectation.container_id}",
                    headers=_bearer(self.manifest.customer.token),
                    params={"workspace": expectation.workspace},
                )
        except (httpx.HTTPError, OSError) as exc:
            return expectation.container_id, type(exc).__name__
        if response.status_code != 200:
            self.state.record_http_error(
                "durable container read",
                response.status_code,
                response.content,
            )
            return expectation.container_id, f"container read returned HTTP {response.status_code}"
        container = ContainerDetailResponse.model_validate(response.json())
        if container.id != expectation.container_id:
            return expectation.container_id, "container response identity mismatch"
        if container.workspace_id != expectation.workspace:
            return expectation.container_id, "container response workspace mismatch"
        if container.runtime_worker_id != self.manifest.delivery_worker.request.worker_id:
            return expectation.container_id, "container durable worker assignment mismatch"
        if container.status not in expectation.allowed_statuses:
            return expectation.container_id, "container durable status mismatch"
        return expectation.container_id, ""

    async def _restart_replica(self) -> None:
        process = await asyncio.create_subprocess_exec(
            *self.config.restart_command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return_code = await asyncio.wait_for(
                process.wait(), timeout=self.config.restart_timeout_seconds
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("replica restart command exceeded its bounded timeout") from None
        if return_code != 0:
            raise RuntimeError(f"replica restart command exited with status {return_code}")
        self._restart_completed = True

    async def _backoff(self, state: _StreamState) -> None:
        if self.stop.is_set():
            return
        exponent = min(state.consecutive_failures, 5)
        await asyncio.sleep(min(0.1 * (2**exponent), _BACKOFF_MAX_SECONDS))

    def _redis_clients_within_limits(self) -> bool:
        if self._redis_baseline is None:
            return True
        peak = self._redis_peak or self._redis_baseline
        connected_limit = self._redis_baseline.connected + int(
            self.config.customer_streams * REDIS_CLIENT_GROWTH_PER_CUSTOMER_STREAM
        )
        return (
            peak.connected <= connected_limit
            and peak.blocked <= self._redis_baseline.blocked + REDIS_BLOCKED_CLIENT_GROWTH_LIMIT
        )

    async def _sample_redis_clients(self) -> _RedisClientSample:
        process = await asyncio.create_subprocess_exec(
            *self.config.redis_info_command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("redis info command exceeded its bounded timeout") from None
        if process.returncode != 0:
            raise RuntimeError(f"redis info command exited with status {process.returncode}")
        fields: dict[str, int] = {}
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            name, separator, value = line.strip().partition(":")
            if separator and name in {"connected_clients", "blocked_clients"} and value.isdigit():
                fields[name] = int(value)
        if len(fields) != 2:
            raise RuntimeError("redis info command output lacks connected and blocked clients")
        return _RedisClientSample(
            connected=fields["connected_clients"],
            blocked=fields["blocked_clients"],
        )

    async def _sustain(self, phase: str, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            counts = self.state.counts(restarted_at=self._restarted_at)
            if self._redis_baseline is not None:
                sample = await self._sample_redis_clients()
                peak = self._redis_peak
                self._redis_peak = sample if peak is None else peak.peak(sample)
                counts = counts | {
                    "redis_clients": sample.connected,
                    "redis_blocked_clients": sample.blocked,
                }
            emit_progress(phase, **counts)
            self._raise_for_request_errors()
            self._raise_for_failed_task()
            if counts["connected"] != len(self.state.streams):
                raise RuntimeError(f"{phase} lost a stream connection")
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(min(_POLL_SECONDS, max(0, deadline - time.monotonic())))

    async def _poll(
        self,
        *,
        phase: str,
        timeout_seconds: float,
        complete: Callable[[dict[str, int]], bool],
        restarted_at: float = 0,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            counts = self.state.counts(restarted_at=restarted_at)
            emit_progress(phase, **counts)
            self._raise_for_request_errors()
            self._raise_for_failed_task()
            if complete(counts):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{phase} did not reach its required stream state")
            await asyncio.sleep(_POLL_SECONDS)

    def _raise_for_failed_task(self) -> None:
        for task in self._tasks:
            if not task.done() or task.cancelled():
                continue
            exception = task.exception()
            if exception is None:
                raise RuntimeError("a stream task stopped before benchmark shutdown")
            raise RuntimeError(
                f"a stream task stopped with {type(exception).__name__}"
            ) from exception

    def _raise_for_request_errors(self) -> None:
        if self.state.request_errors:
            raise RuntimeError(self.state.request_errors[0])

    def _all_expected_deliveries_acknowledged(self) -> bool:
        return self.state.acknowledged_requests == set(self._expected_requests())

    def _restart_was_observed(self) -> bool:
        return all(
            state.connection_attempts > state.attempts_at_restart for state in self.state.streams
        )

    def _expected_requests(self) -> dict[str, DurableContainerExpectation]:
        return {item.container_id: item for item in self.manifest.delivery_worker.expected_requests}

    @staticmethod
    def _safe_failure(exc: Exception) -> str:
        if isinstance(exc, (RuntimeError, TimeoutError, ValueError)) and str(exc):
            return str(exc)[:300]
        return type(exc).__name__


def run_control_plane_stream_benchmark(
    config: ControlPlaneStreamConfig,
) -> ControlPlaneStreamReport:
    if config.manifest is None:
        raise ValueError("a prepared control-plane stream manifest is required")
    manifest = ControlPlaneStreamManifest.model_validate_json(config.manifest.read_text())
    return asyncio.run(_ControlPlaneStreamRun(config, manifest).run())


__all__ = [
    "ControlPlaneStreamConfig",
    "ControlPlaneStreamManifest",
    "ControlPlaneStreamReport",
    "CustomerStreamTarget",
    "DeliveryWorkerStreamIdentity",
    "DurableContainerExpectation",
    "WorkerStreamIdentity",
    "emit_progress",
    "run_control_plane_stream_benchmark",
]

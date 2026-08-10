from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

from foundation.process import ProcessOutputChunk, ProcessOutputStream
from pydantic import field_validator, model_validator
from shared.contracts import ContractModel
from shared.logs import ContainerLogEntryKind

from worker.events import ContainerRequestContext
from worker.repository_payloads import (
    MAX_CONTAINER_LOG_BATCH_ENTRIES,
    MAX_CONTAINER_LOG_MESSAGE_BYTES,
    AppendContainerLogsResponse,
    ContainerLogBatchEntry,
    ContainerLogStream,
)

DEFAULT_CONTAINER_LOG_QUEUE_CAPACITY = 4096
DEFAULT_CONTAINER_LOG_BATCH_SIZE = 128
DEFAULT_CONTAINER_LOG_BATCH_INTERVAL_SECONDS = 0.05
DEFAULT_CONTAINER_LOG_RETRY_INITIAL_SECONDS = 0.1
DEFAULT_CONTAINER_LOG_RETRY_MAX_SECONDS = 2.0
DEFAULT_CONTAINER_LOG_FLUSH_TIMEOUT_SECONDS = 5.0
DEFAULT_CONTAINER_LOG_LINES_PER_HOUR = 360_000
DEFAULT_CONTAINER_LOG_RATE_LIMIT_BURST = 1000
CONTAINER_LOG_DROPPED_MESSAGE = "container log output dropped by worker limits"


class ContainerLogBatchSink(Protocol):
    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse: ...


class ContainerLogCaptureSettings(ContractModel):
    queue_capacity: int = DEFAULT_CONTAINER_LOG_QUEUE_CAPACITY
    batch_size: int = DEFAULT_CONTAINER_LOG_BATCH_SIZE
    batch_interval_seconds: float = DEFAULT_CONTAINER_LOG_BATCH_INTERVAL_SECONDS
    retry_initial_seconds: float = DEFAULT_CONTAINER_LOG_RETRY_INITIAL_SECONDS
    retry_max_seconds: float = DEFAULT_CONTAINER_LOG_RETRY_MAX_SECONDS
    flush_timeout_seconds: float = DEFAULT_CONTAINER_LOG_FLUSH_TIMEOUT_SECONDS
    max_message_bytes: int = MAX_CONTAINER_LOG_MESSAGE_BYTES
    lines_per_hour: int = DEFAULT_CONTAINER_LOG_LINES_PER_HOUR
    rate_limit_burst: int = DEFAULT_CONTAINER_LOG_RATE_LIMIT_BURST

    @field_validator(
        "queue_capacity",
        "batch_size",
        "max_message_bytes",
        "lines_per_hour",
        "rate_limit_burst",
    )
    @classmethod
    def positive_counts(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("container log capture counts must be positive")
        return value

    @field_validator(
        "batch_interval_seconds",
        "retry_initial_seconds",
        "retry_max_seconds",
        "flush_timeout_seconds",
    )
    @classmethod
    def positive_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("container log capture timing must be positive")
        return value

    @field_validator("batch_size")
    @classmethod
    def batch_fits_ingestion_contract(cls, value: int) -> int:
        if value > MAX_CONTAINER_LOG_BATCH_ENTRIES:
            raise ValueError(
                f"container log batches cannot exceed {MAX_CONTAINER_LOG_BATCH_ENTRIES} entries"
            )
        return value

    @field_validator("max_message_bytes")
    @classmethod
    def message_fits_ingestion_contract(cls, value: int) -> int:
        if value > MAX_CONTAINER_LOG_MESSAGE_BYTES:
            raise ValueError(
                f"container log messages cannot exceed {MAX_CONTAINER_LOG_MESSAGE_BYTES} bytes"
            )
        if value < 4:
            raise ValueError("container log message limit must fit one UTF-8 code point")
        return value

    @model_validator(mode="after")
    def retry_range_is_ordered(self) -> ContainerLogCaptureSettings:
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("container log maximum retry must not be below initial retry")
        return self


class ContainerLogCaptureResult(ContractModel):
    container_id: str
    capture_id: str
    accepted_through: int = -1
    output_entries: int = 0
    dropped_entries: int = 0
    delivery_attempts: int = 0
    delivery_failures: int = 0
    pending_entries: int = 0
    flushed: bool = False
    last_error: str = ""


@dataclass(slots=True)
class WorkerContainerLogCaptureService:
    sink: ContainerLogBatchSink
    settings: ContainerLogCaptureSettings = field(default_factory=ContainerLogCaptureSettings)
    capture_id_factory: Callable[[], str] = field(
        default=lambda: str(uuid4()),
        repr=False,
    )

    def begin(
        self,
        request: ContainerRequestContext,
        *,
        capture_id: str | None = None,
        starting_sequence: int = 0,
    ) -> ContainerLogCaptureHandle:
        if starting_sequence < 0:
            raise ValueError("container log starting sequence cannot be negative")
        resolved_capture_id = (capture_id or self.capture_id_factory()).strip()
        if not resolved_capture_id:
            raise ValueError("container log capture id cannot be empty")
        handle = ContainerLogCaptureHandle(
            container_id=request.container_id,
            capture_id=resolved_capture_id,
            sink=self.sink,
            settings=self.settings,
            starting_sequence=starting_sequence,
        )
        handle.start()
        return handle


@dataclass(slots=True)
class ContainerLogCaptureHandle:
    container_id: str
    capture_id: str
    sink: ContainerLogBatchSink
    settings: ContainerLogCaptureSettings
    starting_sequence: int = 0
    _condition: threading.Condition = field(
        default_factory=threading.Condition,
        init=False,
        repr=False,
    )
    _pending: deque[ContainerLogBatchEntry] = field(
        default_factory=deque,
        init=False,
        repr=False,
    )
    _partial: dict[ContainerLogStream, str] = field(
        default_factory=lambda: {
            ContainerLogStream.Stdout: "",
            ContainerLogStream.Stderr: "",
        },
        init=False,
        repr=False,
    )
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _next_sequence: int = field(init=False, repr=False)
    _accepted_through: int = field(init=False, repr=False)
    _accepting: bool = field(default=True, init=False, repr=False)
    _close_requested: bool = field(default=False, init=False, repr=False)
    _close_deadline: float | None = field(default=None, init=False, repr=False)
    _flush_sequence: int | None = field(default=None, init=False, repr=False)
    _flushed: bool = field(default=False, init=False, repr=False)
    _stopped: bool = field(default=False, init=False, repr=False)
    _pending_dropped: int = field(default=0, init=False, repr=False)
    _pending_dropped_at: datetime | None = field(default=None, init=False, repr=False)
    _output_entries: int = field(default=0, init=False, repr=False)
    _dropped_entries: int = field(default=0, init=False, repr=False)
    _delivery_attempts: int = field(default=0, init=False, repr=False)
    _delivery_failures: int = field(default=0, init=False, repr=False)
    _last_error: str = field(default="", init=False, repr=False)
    _rate_tokens: float = field(init=False, repr=False)
    _rate_updated_at: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.container_id.strip():
            raise ValueError("container log capture requires a container id")
        if not self.capture_id.strip():
            raise ValueError("container log capture requires a capture id")
        if self.starting_sequence < 0:
            raise ValueError("container log starting sequence cannot be negative")
        self._next_sequence = self.starting_sequence
        self._accepted_through = self.starting_sequence - 1
        self._rate_tokens = float(self.settings.rate_limit_burst)
        self._rate_updated_at = monotonic()

    @property
    def process_output_sink(self) -> Callable[[ProcessOutputChunk], None]:
        return self.capture

    def start(self) -> None:
        with self._condition:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._deliver,
                name=f"container-log-capture-{self.container_id}",
                daemon=True,
            )
            self._thread.start()

    def record_diagnostic(self, message: str) -> None:
        """Record why a container produced no output of its own.

        Typed as a diagnostic rather than output so a worker-authored explanation
        is never presented as something the container printed.
        """
        text = message.strip()
        if not text:
            return
        with self._condition:
            if not self._accepting:
                return
            self._pending.append(
                ContainerLogBatchEntry(
                    sequence=self._claim_sequence(),
                    stream=ContainerLogStream.Stderr,
                    message=text[:4000],
                    timestamp=datetime.now(UTC),
                    kind=ContainerLogEntryKind.Diagnostic,
                )
            )
            self._condition.notify_all()

    def capture(self, chunk: ProcessOutputChunk) -> None:
        stream = _container_log_stream(chunk.stream)
        with self._condition:
            if not self._accepting or not chunk.text:
                return
            self._partial[stream] += chunk.text
            self._enqueue_complete_lines(stream)
            self._condition.notify_all()

    def flush(self, *, timeout_seconds: float | None = None) -> ContainerLogCaptureResult:
        return self.close(timeout_seconds=timeout_seconds)

    def close(self, *, timeout_seconds: float | None = None) -> ContainerLogCaptureResult:
        timeout = (
            self.settings.flush_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if timeout <= 0:
            raise ValueError("container log flush timeout must be positive")
        deadline = monotonic() + timeout
        with self._condition:
            if not self._close_requested:
                self._accepting = False
                self._enqueue_partial_lines()
                self._close_requested = True
                self._close_deadline = deadline
                self._materialize_control_entries()
                self._condition.notify_all()
            elif self._close_deadline is not None:
                self._close_deadline = max(self._close_deadline, deadline)

            while not self._stopped and monotonic() < deadline:
                self._condition.wait(timeout=max(deadline - monotonic(), 0))

            return self._result()

    def result(self) -> ContainerLogCaptureResult:
        with self._condition:
            return self._result()

    def _enqueue_complete_lines(self, stream: ContainerLogStream) -> None:
        buffered = self._partial[stream]
        while "\n" in buffered:
            line, buffered = buffered.split("\n", 1)
            self._enqueue_message(stream, line.removesuffix("\r"))
        pieces, buffered = _bounded_message_pieces(
            buffered,
            max_bytes=self.settings.max_message_bytes,
            keep_final=True,
        )
        for piece in pieces:
            self._enqueue_message(stream, piece)
        self._partial[stream] = buffered

    def _enqueue_partial_lines(self) -> None:
        for stream in (ContainerLogStream.Stdout, ContainerLogStream.Stderr):
            buffered = self._partial[stream]
            pieces, _ = _bounded_message_pieces(
                buffered,
                max_bytes=self.settings.max_message_bytes,
                keep_final=False,
            )
            for piece in pieces:
                self._enqueue_message(stream, piece.removesuffix("\r"))
            self._partial[stream] = ""

    def _enqueue_message(self, stream: ContainerLogStream, message: str) -> None:
        if not message:
            return
        if not self._allow_line():
            self._record_drop()
            return
        self._materialize_control_entries()
        if len(self._pending) >= self.settings.queue_capacity:
            self._record_drop()
            return
        self._pending.append(
            ContainerLogBatchEntry(
                sequence=self._claim_sequence(),
                stream=stream,
                message=message,
                timestamp=datetime.now(UTC),
            )
        )
        self._output_entries += 1

    def _allow_line(self) -> bool:
        current = monotonic()
        refill_per_second = self.settings.lines_per_hour / 3600
        self._rate_tokens = min(
            float(self.settings.rate_limit_burst),
            self._rate_tokens + (current - self._rate_updated_at) * refill_per_second,
        )
        self._rate_updated_at = current
        if self._rate_tokens < 1:
            return False
        self._rate_tokens -= 1
        return True

    def _record_drop(self) -> None:
        self._pending_dropped += 1
        self._dropped_entries += 1
        if self._pending_dropped_at is None:
            self._pending_dropped_at = datetime.now(UTC)

    def _materialize_control_entries(self) -> None:
        if self._pending_dropped and len(self._pending) < self.settings.queue_capacity:
            self._pending.append(
                ContainerLogBatchEntry(
                    sequence=self._claim_sequence(),
                    stream=ContainerLogStream.Stdout,
                    message=CONTAINER_LOG_DROPPED_MESSAGE,
                    timestamp=self._pending_dropped_at or datetime.now(UTC),
                    kind=ContainerLogEntryKind.Dropped,
                    dropped_count=self._pending_dropped,
                )
            )
            self._pending_dropped = 0
            self._pending_dropped_at = None
        if (
            self._close_requested
            and not self._pending_dropped
            and self._flush_sequence is None
            and len(self._pending) < self.settings.queue_capacity
        ):
            sequence = self._claim_sequence()
            self._pending.append(
                ContainerLogBatchEntry(
                    sequence=sequence,
                    stream=ContainerLogStream.Stdout,
                    timestamp=datetime.now(UTC),
                    kind=ContainerLogEntryKind.Flush,
                )
            )
            self._flush_sequence = sequence

    def _claim_sequence(self) -> int:
        sequence = self._next_sequence
        self._next_sequence += 1
        return sequence

    def _deliver(self) -> None:
        retry_seconds = self.settings.retry_initial_seconds
        batch: tuple[ContainerLogBatchEntry, ...] | None = None
        while True:
            if batch is None:
                batch = self._next_batch()
                if batch is None:
                    return
            # Repository failures must never block the two process reader threads.
            try:
                response = self.sink.publish_container_logs(
                    container_id=self.container_id,
                    capture_id=self.capture_id,
                    entries=list(batch),
                )
            except Exception as exc:
                with self._condition:
                    self._delivery_attempts += 1
                    self._delivery_failures += 1
                    self._last_error = f"{type(exc).__name__}: {exc}"
                    if self._delivery_deadline_expired():
                        self._stop_delivery()
                        return
                    wait_seconds = self._bounded_retry_wait(retry_seconds)
                    self._condition.wait(timeout=wait_seconds)
                retry_seconds = min(retry_seconds * 2, self.settings.retry_max_seconds)
                continue

            with self._condition:
                self._delivery_attempts += 1
                if not (batch[0].sequence <= response.accepted_through <= batch[-1].sequence):
                    self._delivery_failures += 1
                    self._last_error = (
                        "container log sink acknowledgement is outside submitted batch "
                        f"{batch[0].sequence}..{batch[-1].sequence}: "
                        f"{response.accepted_through}"
                    )
                    if self._delivery_deadline_expired():
                        self._stop_delivery()
                        return
                    wait_seconds = self._bounded_retry_wait(retry_seconds)
                    self._condition.wait(timeout=wait_seconds)
                    retry_seconds = min(retry_seconds * 2, self.settings.retry_max_seconds)
                    continue

                retry_seconds = self.settings.retry_initial_seconds
                self._accepted_through = max(
                    self._accepted_through,
                    response.accepted_through,
                )
                while self._pending and self._pending[0].sequence <= response.accepted_through:
                    self._pending.popleft()
                batch = None
                self._materialize_control_entries()
                if (
                    self._flush_sequence is not None
                    and self._accepted_through >= self._flush_sequence
                ):
                    self._flushed = True
                    self._stop_delivery()
                    return
                self._condition.notify_all()

    def _next_batch(self) -> tuple[ContainerLogBatchEntry, ...] | None:
        with self._condition:
            while True:
                self._materialize_control_entries()
                if self._pending:
                    if not self._close_requested:
                        self._condition.wait(timeout=self.settings.batch_interval_seconds)
                        self._materialize_control_entries()
                    count = min(len(self._pending), self.settings.batch_size)
                    return tuple(self._pending[index] for index in range(count))
                if self._delivery_deadline_expired():
                    self._stop_delivery()
                    return None
                self._condition.wait(timeout=self._deadline_wait_seconds())

    def _delivery_deadline_expired(self) -> bool:
        return (
            self._close_requested
            and self._close_deadline is not None
            and monotonic() >= self._close_deadline
        )

    def _deadline_wait_seconds(self) -> float | None:
        if not self._close_requested or self._close_deadline is None:
            return None
        return max(self._close_deadline - monotonic(), 0)

    def _bounded_retry_wait(self, retry_seconds: float) -> float:
        deadline_wait = self._deadline_wait_seconds()
        if deadline_wait is None:
            return retry_seconds
        return min(retry_seconds, deadline_wait)

    def _stop_delivery(self) -> None:
        self._stopped = True
        self._condition.notify_all()

    def _result(self) -> ContainerLogCaptureResult:
        return ContainerLogCaptureResult(
            container_id=self.container_id,
            capture_id=self.capture_id,
            accepted_through=self._accepted_through,
            output_entries=self._output_entries,
            dropped_entries=self._dropped_entries,
            delivery_attempts=self._delivery_attempts,
            delivery_failures=self._delivery_failures,
            pending_entries=len(self._pending) + int(self._pending_dropped > 0),
            flushed=self._flushed,
            last_error=self._last_error,
        )


def _container_log_stream(stream: ProcessOutputStream) -> ContainerLogStream:
    if stream is ProcessOutputStream.Stderr:
        return ContainerLogStream.Stderr
    return ContainerLogStream.Stdout


def _bounded_message_pieces(
    message: str,
    *,
    max_bytes: int,
    keep_final: bool,
) -> tuple[list[str], str]:
    encoded = message.encode("utf-8")
    pieces: list[str] = []
    while len(encoded) > max_bytes or (encoded and not keep_final):
        if len(encoded) <= max_bytes:
            pieces.append(encoded.decode("utf-8"))
            encoded = b""
            break
        cut = max_bytes
        while cut > 0 and encoded[cut] & 0b1100_0000 == 0b1000_0000:
            cut -= 1
        if cut == 0:
            raise ValueError("container log message limit split an invalid UTF-8 sequence")
        pieces.append(encoded[:cut].decode("utf-8"))
        encoded = encoded[cut:]
    return pieces, encoded.decode("utf-8")

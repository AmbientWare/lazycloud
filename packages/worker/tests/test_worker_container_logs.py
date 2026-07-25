from __future__ import annotations

import threading
from dataclasses import dataclass, field

from foundation.process import ProcessOutputChunk, ProcessOutputStream
from worker.container_logs import (
    ContainerLogCaptureSettings,
    WorkerContainerLogCaptureService,
)
from worker.events import ContainerRequestContext
from worker.repository_payloads import (
    AppendContainerLogsResponse,
    ContainerLogBatchEntry,
    ContainerLogEntryKind,
    ContainerLogStream,
)


@dataclass(slots=True)
class RecordingLogSink:
    failures_remaining: int = 0
    requests: list[tuple[str, str, tuple[ContainerLogBatchEntry, ...]]] = field(
        default_factory=list
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse:
        request = (container_id, capture_id, tuple(entries))
        with self._lock:
            self.requests.append(request)
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise RuntimeError("repository unavailable")
        return AppendContainerLogsResponse(
            accepted_through=entries[-1].sequence,
            appended_count=len(entries),
        )

    def entries(self) -> list[ContainerLogBatchEntry]:
        accepted: dict[int, ContainerLogBatchEntry] = {}
        with self._lock:
            for _, _, entries in self.requests:
                for entry in entries:
                    accepted[entry.sequence] = entry
        return [accepted[sequence] for sequence in sorted(accepted)]


@dataclass(slots=True)
class BlockingLogSink:
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    requests: list[tuple[ContainerLogBatchEntry, ...]] = field(default_factory=list)

    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse:
        del container_id, capture_id
        self.requests.append(tuple(entries))
        self.started.set()
        assert self.release.wait(timeout=2)
        return AppendContainerLogsResponse(
            accepted_through=entries[-1].sequence,
            appended_count=len(entries),
        )


@dataclass(slots=True)
class UnavailableLogSink:
    attempts: int = 0

    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse:
        del container_id, capture_id, entries
        self.attempts += 1
        raise RuntimeError("repository unavailable")


@dataclass(slots=True)
class InvalidAckLogSink:
    attempts: int = 0

    def publish_container_logs(
        self,
        *,
        container_id: str,
        capture_id: str,
        entries: list[ContainerLogBatchEntry],
    ) -> AppendContainerLogsResponse:
        del container_id, capture_id
        self.attempts += 1
        return AppendContainerLogsResponse(
            accepted_through=entries[-1].sequence + 1,
            appended_count=0,
        )


def _request() -> ContainerRequestContext:
    return ContainerRequestContext(container_id="ctr-1", workspace_id="ws-1")


def _settings(**updates: int | float) -> ContainerLogCaptureSettings:
    return ContainerLogCaptureSettings.model_validate(
        {
            "batch_interval_seconds": 0.001,
            "retry_initial_seconds": 0.001,
            "retry_max_seconds": 0.005,
            "flush_timeout_seconds": 1,
            **updates,
        }
    )


def test_container_log_capture_frames_partial_lines_and_preserves_streams() -> None:
    sink = RecordingLogSink()
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(),
        capture_id_factory=lambda: "capture-1",
    ).begin(_request())

    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "first\npartial"))
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stderr, "failure\r\n"))
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "-line\nlast"))
    result = handle.close()

    assert result.flushed
    assert result.capture_id == "capture-1"
    entries = sink.entries()
    assert [entry.sequence for entry in entries] == [0, 1, 2, 3, 4]
    assert [entry.kind for entry in entries] == [
        ContainerLogEntryKind.Output,
        ContainerLogEntryKind.Output,
        ContainerLogEntryKind.Output,
        ContainerLogEntryKind.Output,
        ContainerLogEntryKind.Flush,
    ]
    assert [(entry.stream, entry.message) for entry in entries[:-1]] == [
        (ContainerLogStream.Stdout, "first"),
        (ContainerLogStream.Stderr, "failure"),
        (ContainerLogStream.Stdout, "partial-line"),
        (ContainerLogStream.Stdout, "last"),
    ]


def test_container_log_capture_splits_utf8_without_breaking_code_points() -> None:
    sink = RecordingLogSink()
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(max_message_bytes=5),
    ).begin(_request(), capture_id="utf8-capture")

    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "ééé"))
    result = handle.close()

    assert result.flushed
    output = [entry for entry in sink.entries() if entry.kind is ContainerLogEntryKind.Output]
    assert [entry.message for entry in output] == ["éé", "é"]
    assert all(len(entry.message.encode("utf-8")) <= 5 for entry in output)


def test_container_log_capture_close_materializes_drop_and_flush_when_queue_full() -> None:
    sink = BlockingLogSink()
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(queue_capacity=2, batch_size=2),
    ).begin(_request(), capture_id="bounded-capture")
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "line-0\n"))
    assert sink.started.wait(timeout=1)

    for index in range(1, 6):
        handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, f"line-{index}\n"))

    sink.release.set()
    result = handle.close()

    assert result.flushed
    assert result.output_entries == 2
    assert result.dropped_entries == 4
    entries = {entry.sequence: entry for request in sink.requests for entry in request}
    ordered = [entries[sequence] for sequence in sorted(entries)]
    assert [entry.sequence for entry in ordered] == list(range(len(ordered)))
    dropped = next(entry for entry in ordered if entry.kind is ContainerLogEntryKind.Dropped)
    assert dropped.dropped_count == 4
    assert ordered[-1].kind is ContainerLogEntryKind.Flush


def test_container_log_capture_retries_the_same_idempotent_batch() -> None:
    sink = RecordingLogSink(failures_remaining=1)
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(),
    ).begin(_request(), capture_id="retry-capture")
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "retry-me\n"))
    result = handle.close()

    assert result.flushed
    assert result.delivery_failures == 1
    assert len(sink.requests) >= 2
    assert sink.requests[0] == sink.requests[1]
    assert sink.entries()[-1].kind is ContainerLogEntryKind.Flush


def test_container_log_capture_rate_limit_is_reported_without_sequence_gap() -> None:
    sink = RecordingLogSink()
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(lines_per_hour=1, rate_limit_burst=1),
    ).begin(_request(), capture_id="limited-capture")

    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "accepted\ndropped\n"))
    result = handle.close()

    assert result.flushed
    assert result.output_entries == 1
    assert result.dropped_entries == 1
    entries = sink.entries()
    assert [entry.sequence for entry in entries] == [0, 1, 2]
    assert [entry.kind for entry in entries] == [
        ContainerLogEntryKind.Output,
        ContainerLogEntryKind.Dropped,
        ContainerLogEntryKind.Flush,
    ]


def test_container_log_capture_outage_stops_at_flush_deadline() -> None:
    sink = UnavailableLogSink()
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(flush_timeout_seconds=0.04),
    ).begin(_request(), capture_id="outage-capture")
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "not-delivered\n"))

    result = handle.close()

    assert not result.flushed
    assert result.pending_entries == 2
    assert result.delivery_failures >= 1
    assert sink.attempts == result.delivery_attempts
    assert result.last_error == "RuntimeError: repository unavailable"


def test_container_log_capture_rejects_ack_beyond_submitted_batch() -> None:
    sink = InvalidAckLogSink()
    handle = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(flush_timeout_seconds=0.04),
    ).begin(_request(), capture_id="invalid-ack-capture")
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stdout, "not-acknowledged\n"))

    result = handle.close()

    assert not result.flushed
    assert result.accepted_through == -1
    assert result.pending_entries == 2
    assert result.delivery_failures == sink.attempts
    assert "outside submitted batch" in result.last_error


def test_container_log_capture_can_resume_an_explicit_capture_sequence() -> None:
    sink = RecordingLogSink()
    service = WorkerContainerLogCaptureService(
        sink=sink,
        settings=_settings(),
    )
    handle = service.begin(
        _request(),
        capture_id="restored-capture",
        starting_sequence=8,
    )
    handle.capture(ProcessOutputChunk(ProcessOutputStream.Stderr, "after-restore\n"))

    result = handle.close()

    assert result.flushed
    assert [entry.sequence for entry in sink.entries()] == [8, 9]
    assert sink.entries()[0].stream is ContainerLogStream.Stderr

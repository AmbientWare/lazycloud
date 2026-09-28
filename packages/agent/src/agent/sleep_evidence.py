from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from shared.capacity_lifecycle import CapacitySleepObservation, CapacitySleepRequest
from shared.contracts import ContractModel

from agent.state import model_payload, read_boot_id, write_json_atomic
from agent.suspend import SLEEP_THRESHOLD_SECONDS, sleep_offset

KERNEL_MESSAGES_PATH = Path("/dev/kmsg")


class AgentSleepMarkerError(RuntimeError):
    """The attempt cannot establish a current kernel marker."""


class _SleepRecord(ContractModel):
    request: CapacitySleepRequest
    sleep_offset: float
    marker_written: bool = False
    observation: CapacitySleepObservation | None = None
    acknowledged: CapacitySleepObservation | None = None


@dataclass(slots=True)
class AgentSleepEvidence:
    state_dir: Path

    @property
    def path(self) -> Path:
        return self.state_dir / "capacity-sleep.json"

    def _load(self) -> _SleepRecord | None:
        if not self.path.exists():
            return None
        return _SleepRecord.model_validate_json(self.path.read_text(encoding="utf-8"))

    def _save(self, record: _SleepRecord) -> None:
        write_json_atomic(self.path, model_payload(record), permissions=0o600, durable=True)

    def prepare(self, request: CapacitySleepRequest) -> None:
        record = self._load()
        if record is not None:
            if request.attempt_id == record.request.attempt_id:
                if request != record.request:
                    raise ValueError("sleep attempt changed after preparation")
                if not record.marker_written:
                    raise AgentSleepMarkerError(
                        "sleep marker was interrupted; a new attempt is required"
                    )
                return
            if request.requested_at <= record.request.requested_at:
                return
            if record.observation != record.acknowledged:
                raise RuntimeError("sleep evidence must be acknowledged before another attempt")
        boot_id = read_boot_id()
        if not boot_id or request.boot_id != boot_id:
            raise ValueError("sleep request does not match the current boot")
        attempt_id = UUID(request.attempt_id)
        boot_uuid = UUID(boot_id)
        record = _SleepRecord(request=request, sleep_offset=sleep_offset())
        # A restart in the marker write window must not emit a second marker or
        # acknowledge evidence whose presence in the kernel log is uncertain.
        self._save(record)
        try:
            with KERNEL_MESSAGES_PATH.open("w", encoding="utf-8") as stream:
                stream.write(f"<3>lazycloud-sleep attempt={attempt_id} boot={boot_uuid}\n")
        except OSError as exc:
            raise AgentSleepMarkerError(
                "sleep marker could not be written; a new attempt is required"
            ) from exc
        record.marker_written = True
        record.observation = CapacitySleepObservation(
            attempt_id=request.attempt_id, boot_id=boot_id
        )
        self._save(record)

    def observation(self) -> CapacitySleepObservation | None:
        record = self._load()
        if record is None or not record.marker_written:
            return None
        boot_id = read_boot_id()
        if not boot_id:
            raise RuntimeError("sleep evidence requires the current kernel boot identity")
        suspended = (
            max(0.0, sleep_offset() - record.sleep_offset)
            if boot_id == record.request.boot_id
            else 0.0
        )
        previous = record.observation
        # Clock reads have small sampling error. Freeze the measured restoration
        # once recorded so retries remain identical until acknowledged.
        if (
            previous is None
            or previous.boot_id != boot_id
            or (previous.suspended_seconds == 0 and suspended > SLEEP_THRESHOLD_SECONDS)
        ):
            record.observation = CapacitySleepObservation(
                attempt_id=record.request.attempt_id,
                boot_id=boot_id,
                suspended_seconds=suspended if suspended > SLEEP_THRESHOLD_SECONDS else 0.0,
            )
            self._save(record)
        return record.observation if record.observation != record.acknowledged else None

    def acknowledge(self, attempt_id: str, sent: CapacitySleepObservation | None) -> None:
        if not attempt_id or sent is None or sent.attempt_id != attempt_id:
            return
        record = self._load()
        if record is None or record.request.attempt_id != attempt_id:
            return
        record.acknowledged = sent
        self._save(record)

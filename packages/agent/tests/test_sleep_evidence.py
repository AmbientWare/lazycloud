from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agent.sleep_evidence import AgentSleepEvidence, AgentSleepMarkerError
from shared.capacity_lifecycle import CapacitySleepMode, CapacitySleepRequest

BOOT_ID = "11111111-1111-4111-8111-111111111111"
ATTEMPT_ID = "22222222-2222-4222-8222-222222222222"


def test_sleep_evidence_survives_restart_and_acknowledges_only_the_sent_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_path = tmp_path / "boot_id"
    boot_path.write_text(BOOT_ID)
    monkeypatch.setattr("agent.state.BOOT_ID_PATH", boot_path)
    monkeypatch.setattr("agent.sleep_evidence.KERNEL_MESSAGES_PATH", tmp_path / "kmsg")
    monkeypatch.setattr("agent.sleep_evidence.sleep_offset", lambda: 0.0)
    request = CapacitySleepRequest(
        attempt_id=ATTEMPT_ID,
        boot_id=BOOT_ID,
        mode=CapacitySleepMode.Hibernate,
        requested_at=datetime.now(UTC),
    )
    evidence = AgentSleepEvidence(tmp_path)
    evidence.prepare(request)
    prepared = evidence.observation()
    assert prepared is not None
    assert prepared.suspended_seconds == 0
    evidence.acknowledge(ATTEMPT_ID, prepared)
    assert evidence.observation() is None

    monkeypatch.setattr("agent.sleep_evidence.sleep_offset", lambda: 60.0)
    restarted = AgentSleepEvidence(tmp_path)
    restored = restarted.observation()
    assert restored is not None
    assert restored.suspended_seconds == 60
    restarted.acknowledge(ATTEMPT_ID, prepared)
    assert restarted.observation() == restored
    restarted.prepare(request)
    restarted.prepare(
        request.model_copy(
            update={
                "attempt_id": "33333333-3333-4333-8333-333333333333",
                "requested_at": request.requested_at - timedelta(seconds=1),
            }
        )
    )
    assert restarted.observation() == restored
    restarted.acknowledge(ATTEMPT_ID, restored)
    assert restarted.observation() is None


def test_cold_boot_reports_the_attempt_without_worker_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_path = tmp_path / "boot_id"
    boot_path.write_text(BOOT_ID)
    monkeypatch.setattr("agent.state.BOOT_ID_PATH", boot_path)
    monkeypatch.setattr("agent.sleep_evidence.KERNEL_MESSAGES_PATH", tmp_path / "kmsg")
    evidence = AgentSleepEvidence(tmp_path)
    request = CapacitySleepRequest(
        attempt_id=ATTEMPT_ID,
        boot_id=BOOT_ID,
        mode=CapacitySleepMode.Stop,
        requested_at=datetime.now(UTC),
    )
    evidence.prepare(request)
    evidence.acknowledge(ATTEMPT_ID, evidence.observation())
    boot_path.write_text("44444444-4444-4444-8444-444444444444")
    restarted = AgentSleepEvidence(tmp_path)
    observed = restarted.observation()
    assert observed is not None
    assert observed.attempt_id == ATTEMPT_ID
    assert observed.boot_id == boot_path.read_text()
    assert observed.suspended_seconds == 0
    assert AgentSleepEvidence(tmp_path).observation() == observed


def test_missing_kernel_marker_never_acknowledges_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot_path = tmp_path / "boot_id"
    boot_path.write_text(BOOT_ID)
    monkeypatch.setattr("agent.state.BOOT_ID_PATH", boot_path)
    monkeypatch.setattr("agent.sleep_evidence.KERNEL_MESSAGES_PATH", tmp_path)
    request = CapacitySleepRequest(
        attempt_id=ATTEMPT_ID,
        boot_id=BOOT_ID,
        mode=CapacitySleepMode.Hibernate,
        requested_at=datetime.now(UTC),
    )
    evidence = AgentSleepEvidence(tmp_path)
    with pytest.raises(AgentSleepMarkerError, match="could not be written"):
        evidence.prepare(request)
    assert evidence.observation() is None
    monkeypatch.setattr("agent.sleep_evidence.KERNEL_MESSAGES_PATH", tmp_path / "kmsg")
    with pytest.raises(AgentSleepMarkerError, match="a new attempt is required"):
        AgentSleepEvidence(tmp_path).prepare(request)
    assert evidence.observation() is None

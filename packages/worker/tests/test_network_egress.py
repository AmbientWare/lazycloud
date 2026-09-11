from datetime import timedelta

import pytest
from shared.http.worker_network import WorkerEgressPolicy
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from shared.worker_events import WorkerEventRecord
from worker.network_egress import WorkerNetworkEgressCounters


class EventSink:
    def append(self, record: WorkerEventRecord) -> WorkerEventRecord:
        raise AssertionError("route evidence should remain available during this test")


def test_expired_route_evidence_refuses_container_setup_and_metering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_at = utc_now()
    counters = WorkerNetworkEgressCounters(
        worker_id="worker",
        event_sink=EventSink(),
        load_policy=lambda: WorkerEgressPolicy(
            billing_owner=UsageBillingOwner.ConnectedCloud,
            verified_at=verified_at,
        ),
    )
    counters.initialize()
    try:
        counters.ensure("container", ipv4_interface="eth0", ipv6_interface="")
        assert counters.sample("container").total_bytes == 0
        monkeypatch.setattr(
            "worker.network_egress.utc_now", lambda: verified_at + timedelta(seconds=60)
        )
        with pytest.raises(RuntimeError, match="route evidence is not current"):
            counters.ensure("container", ipv4_interface="eth0", ipv6_interface="")
        with pytest.raises(RuntimeError, match="route evidence is not current"):
            counters.sample("container")
    finally:
        counters.close()

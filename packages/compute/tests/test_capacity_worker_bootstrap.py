from __future__ import annotations

from uuid import uuid4

import pytest
from compute.agent_control import (
    WorkerTokenRecord,
    generate_compute_token,
    plan_agent_worker_token,
)
from compute.bootstrap import MachineBootstrapConfig
from compute.state import ComputeAgentWorkerSlotState
from pydantic import ValidationError
from shared.compute_policy import MachinePool
from shared.scheduling import SchedulerWorkerRecord


@pytest.mark.parametrize(
    "gateway_url",
    [
        "http://control.example.com",
        "https://user:secret@control.example.com",
        "https://control.example.com/path",
        "https://control.example.com?token=abc",
    ],
)
def test_machine_bootstrap_rejects_credentialed_or_non_https_gateway_urls(
    gateway_url: str,
) -> None:
    with pytest.raises(ValidationError, match="gateway_url"):
        MachineBootstrapConfig(
            registration_token="join-token",
            machine_id="machine-1",
            gateway_url=gateway_url,
        )


@pytest.mark.parametrize(
    "capacity_owner_id", ["", "not-a-uuid", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"]
)
def test_registered_worker_boundaries_reject_invalid_capacity_owner_id(
    capacity_owner_id: str,
) -> None:
    with pytest.raises(ValidationError, match="capacity_owner_id"):
        SchedulerWorkerRecord(
            worker_id="worker-1",
            pool="default",
            capacity_owner_id=capacity_owner_id,
        )


def test_agent_worker_token_reuse_requires_reusable_worker_binding() -> None:
    worker_id = str(uuid4())
    token_id = str(uuid4())
    raw_token = generate_compute_token()
    slot = ComputeAgentWorkerSlotState(
        workspace_id=str(uuid4()),
        pool=MachinePool("default"),
        machine_id=str(uuid4()),
        worker_id=worker_id,
        capacity_owner_id=str(uuid4()),
        worker_token_id=token_id,
    )

    accepted = plan_agent_worker_token(
        slot,
        expected_worker_id=worker_id,
        existing_token=WorkerTokenRecord(
            key=raw_token,
            external_id=token_id,
            worker_id=worker_id,
            reusable=True,
        ),
    )
    wrong_binding = plan_agent_worker_token(
        slot,
        expected_worker_id=worker_id,
        existing_token=WorkerTokenRecord(
            key=raw_token,
            external_id=token_id,
            worker_id=str(uuid4()),
            reusable=True,
        ),
    )

    assert accepted.accepted
    assert accepted.reused_existing
    assert not wrong_binding.accepted
    assert wrong_binding.should_create


@pytest.mark.parametrize(
    ("host", "reachable"),
    [
        # A LAN address carries dots and is not loopback, so a name-shaped check
        # accepts it. A remote machine enrols against it, reports healthy, and
        # crash-loops its worker on an origin only the control plane can reach.
        ("10.0.0.150", False),
        ("192.168.1.5", False),
        ("169.254.1.1", False),
        ("control-plane", False),
        # Tailscale's own ranges are what a remote machine actually reaches.
        # The IPv6 prefix is a ULA, which classifies as private, so refusing
        # every private address would reject a working tailnet.
        ("100.69.8.117", True),
        ("fd7a:115c:a1e0::8132:174", True),
        ("fd00::1", False),
        ("52.1.2.3", True),
    ],
)
def test_remote_pool_runtime_callback_rejects_hosts_no_remote_machine_can_reach(
    host: str,
    reachable: bool,
) -> None:
    from compute.agent_control import host_is_unreachable_from_a_remote_machine

    assert host_is_unreachable_from_a_remote_machine(host) is not reachable

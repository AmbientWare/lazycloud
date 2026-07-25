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
            pool_name="default",
            capacity_owner_id=capacity_owner_id,
        )


def test_agent_worker_token_reuse_requires_reusable_worker_binding() -> None:
    worker_id = str(uuid4())
    token_id = str(uuid4())
    raw_token = generate_compute_token()
    slot = ComputeAgentWorkerSlotState(
        workspace_id=str(uuid4()),
        pool_name="default",
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

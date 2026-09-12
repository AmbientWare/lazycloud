from __future__ import annotations

import pytest
from agent.operations import (
    AgentBootstrap,
    AgentCapacityOptions,
    AgentDetectedResources,
    AgentGpuDevice,
    AgentWorkerSlot,
    WorkerExecutor,
    WorkerSlotAction,
    parse_cpu_millicores,
    parse_nvidia_smi_gpu_devices,
    plan_worker_slot_reconciliation,
    resolve_agent_capacity,
    same_worker_slot,
    select_agent_gpu_devices,
)
from pydantic import ValidationError
from shared.compute_enrollment import AgentWorkerSlotStatus, PreflightSeverity
from shared.compute_policy import MachinePool
from shared.routing import BackendRouteTransport
from shared.usage import UsageBillingOwner


def test_agent_bootstrap_requires_private_runtime_after_serialization() -> None:
    bootstrap = AgentBootstrap(
        gateway_public_http_url="https://gateway.example.test",
        gateway_runtime_http_url="http://100.96.0.1:9000",
    )
    serialized = bootstrap.model_dump(mode="json")
    assert AgentBootstrap.model_validate(serialized) == bootstrap

    del serialized["gateway_runtime_http_url"]
    with pytest.raises(ValidationError, match="gateway_runtime_http_url"):
        AgentBootstrap.model_validate(serialized)

    serialized["gateway_runtime_http_url"] = bootstrap.gateway_public_http_url
    with pytest.raises(ValidationError, match="WireGuard runtime service"):
        AgentBootstrap.model_validate(serialized)

    serialized = bootstrap.model_dump(mode="json")
    serialized["transport"] = BackendRouteTransport.Direct.value
    with pytest.raises(ValidationError, match="transport"):
        AgentBootstrap.model_validate(serialized)


def test_capacity_selection_respects_requested_limits_and_rejects_host_overcommit() -> None:
    devices = [
        AgentGpuDevice(id="0", uuid="GPU-0", name="NVIDIA L4"),
        AgentGpuDevice(id="1", uuid="GPU-1", name="NVIDIA A10"),
    ]
    selected, explicit, checks = select_agent_gpu_devices(
        AgentCapacityOptions(gpu_ids="GPU-1"),
        devices,
    )
    assert explicit
    assert selected[0].id == "GPU-1"
    assert checks[0].ok

    plan = resolve_agent_capacity(
        AgentCapacityOptions(
            max_cpu="2.5",
            max_memory="1.5GiB",
            max_gpus=1,
        ),
        AgentDetectedResources(cpu_count=4, memory_mb=4096, gpus=devices),
    )
    assert plan.schedulable
    assert plan.capacity.cpu_millicores == 2500
    assert plan.capacity.memory_mb == 1536
    assert plan.capacity.gpus == ["L4"]
    assert plan.capacity.gpu_count == 1

    unschedulable = resolve_agent_capacity(
        AgentCapacityOptions(max_cpu="99", max_memory="99TiB", max_gpus=3),
        AgentDetectedResources(cpu_count=2, memory_mb=1024, gpus=devices),
    )
    assert not unschedulable.schedulable
    assert {check.severity for check in unschedulable.checks} == {PreflightSeverity.Error}

    with pytest.raises(ValueError, match="positive number"):
        parse_cpu_millicores("0")


def test_nvidia_smi_gpu_device_parsing_normalizes_names_and_skips_invalid_rows() -> None:
    devices = parse_nvidia_smi_gpu_devices(
        """
        0, GPU-a, NVIDIA RTX A4000
        malformed
        1, GPU-b, NVIDIA GeForce RTX 4090
        2, GPU-c, 0
        """
    )

    assert [device.id for device in devices] == ["0", "1"]
    assert [device.uuid for device in devices] == ["GPU-a", "GPU-b"]
    assert [device.name for device in devices] == ["A4000", "RTX4090"]


def test_worker_slot_equality_and_reconciliation() -> None:
    active = AgentWorkerSlot(
        worker_id="worker-1",
        worker_token="token-1",
        pool=MachinePool("default"),
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        billing_owner=UsageBillingOwner.SelfHosted,
        machine_id="machine-1",
        cpu_millicores=1000,
        memory_mb=1024,
    )
    unchanged = active.model_copy()
    changed = active.model_copy(update={"memory_mb": 2048})
    new_slot = AgentWorkerSlot(
        worker_id="worker-2",
        pool=MachinePool("default"),
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        billing_owner=UsageBillingOwner.SelfHosted,
    )

    assert same_worker_slot(active, unchanged)
    assert not same_worker_slot(active, changed)

    image_changed = active.model_copy(update={"worker_image": "worker:new"})
    prepare_plan = plan_worker_slot_reconciliation([image_changed], [active])
    assert prepare_plan.actions[0].action is WorkerSlotAction.Prepare

    plan = plan_worker_slot_reconciliation([changed, new_slot], [active])
    assert [(item.worker_id, item.action) for item in plan.actions] == [
        ("worker-1", WorkerSlotAction.Restart),
        ("worker-2", WorkerSlotAction.Start),
    ]
    assert plan.changed

    image_changed = image_changed.model_copy(update={"status": AgentWorkerSlotStatus.Pending})
    switch_plan = plan_worker_slot_reconciliation([image_changed], [active])
    assert switch_plan.actions[0].action is WorkerSlotAction.Restart

    agent_changed = active.model_copy(update={"agent_binary_sha256": "a" * 64})
    assert (
        plan_worker_slot_reconciliation([agent_changed], [active]).actions[0].action
        is WorkerSlotAction.Prepare
    )
    authorized = agent_changed.model_copy(update={"status": AgentWorkerSlotStatus.Pending})
    assert (
        plan_worker_slot_reconciliation([authorized], [active]).actions[0].action
        is WorkerSlotAction.Restart
    )
    assert (
        plan_worker_slot_reconciliation([authorized], [agent_changed]).actions[0].action
        is WorkerSlotAction.Keep
    )

    stop_plan = plan_worker_slot_reconciliation([], [active])
    assert stop_plan.actions[0].action is WorkerSlotAction.Stop

    external = plan_worker_slot_reconciliation(
        [new_slot],
        [active],
        executor=WorkerExecutor.External,
    )
    assert [item.action for item in external.actions] == [
        WorkerSlotAction.Stop,
        WorkerSlotAction.Unsupported,
    ]

    unsupported = plan_worker_slot_reconciliation([new_slot], [], os_name="darwin")
    assert unsupported.actions[0].action is WorkerSlotAction.Unsupported

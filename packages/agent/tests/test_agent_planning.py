from __future__ import annotations

import pytest
from agent.operations import (
    AgentBootstrap,
    AgentCapacityOptions,
    AgentCapacityPlan,
    AgentDetectedResources,
    AgentGpuDevice,
    AgentWorkerSlot,
    WorkerExecutor,
    WorkerSlotAction,
    build_agent_worker_config,
    parse_cpu_millicores,
    parse_memory_mb,
    parse_nvidia_smi_gpu_devices,
    plan_worker_slot_reconciliation,
    resolve_agent_capacity,
    same_worker_slot,
    select_agent_gpu_devices,
    split_csv,
)
from shared.compute_enrollment import PreflightSeverity
from shared.gpu import normalize_gpu_type


def test_agent_capacity_parsing_gpu_selection_and_schedulable_checks() -> None:
    assert parse_cpu_millicores("2.5") == 2500
    assert parse_memory_mb("1.5GiB") == 1536
    assert parse_memory_mb("2gb") == 2000
    assert split_csv("0, GPU-1, ,") == ["0", "GPU-1"]
    assert normalize_gpu_type("NVIDIA L4") == "L4"
    assert normalize_gpu_type("NVIDIA GeForce RTX 4090") == "RTX4090"

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
            max_cpu="3",
            max_memory="2GiB",
            max_gpus=1,
        ),
        AgentDetectedResources(cpu_count=4, memory_mb=4096, gpus=devices),
    )
    assert isinstance(plan, AgentCapacityPlan)
    assert plan.schedulable
    assert plan.capacity.cpu_millicores == 3000
    assert plan.capacity.memory_mb == 2048
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


def test_agent_service_serializes_worker_capacity_without_credentials() -> None:
    bootstrap = AgentBootstrap(
        gateway_public_http_url="http://gateway.example.test:8080",
        gateway_grpc_host="grpc.example.test",
        gateway_grpc_port=7443,
        gateway_grpc_tls=True,
        transport="tailnet",
        image_registry_store="s3",
    )
    slot = AgentWorkerSlot(
        worker_id="worker-1",
        worker_token="token-1",
        pool_name="gpu",
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        machine_id="machine-1",
        cpu_millicores=2500,
        memory_mb=4096,
        gpu="L4",
        gpu_count=1,
        gpu_assignment="0",
        network_prefix="10.10.0.0/24",
        worker_image="worker:latest",
    )

    config = build_agent_worker_config(bootstrap, slot)
    payload = config.model_dump(mode="json")

    assert payload["execution"]["capacity"] == {
        "cpu_millicores": 2500,
        "memory_mib": 4096,
        "gpu_type": "L4",
        "gpu_count": 1,
    }
    assert "token-1" not in str(payload)


def test_worker_slot_equality_and_reconciliation() -> None:
    active = AgentWorkerSlot(
        worker_id="worker-1",
        worker_token="token-1",
        pool_name="default",
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
        machine_id="machine-1",
        cpu_millicores=1000,
        memory_mb=1024,
    )
    unchanged = active.model_copy()
    changed = active.model_copy(update={"memory_mb": 2048})
    new_slot = AgentWorkerSlot(
        worker_id="worker-2",
        pool_name="default",
        capacity_owner_id="11111111-1111-4111-8111-111111111111",
    )

    assert same_worker_slot(active, unchanged)
    assert not same_worker_slot(active, changed)

    plan = plan_worker_slot_reconciliation([changed, new_slot], [active])
    assert [(item.worker_id, item.action) for item in plan.actions] == [
        ("worker-1", WorkerSlotAction.Restart),
        ("worker-2", WorkerSlotAction.Start),
    ]
    assert plan.changed

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

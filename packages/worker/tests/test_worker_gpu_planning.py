from __future__ import annotations

from subprocess import CompletedProcess

from worker.gpu import (
    DynamicGpuAllocationManager,
    GpuAllocationManager,
)

NVIDIA_SMI_OUTPUT = """0x0000, 00000000:23:00.0, 0, GPU-afb8c77a-62ef-a631-48d0-edc9670fef25
0x0000, 00000000:41:00.0, 1, GPU-c79e0183-59ff-a978-4cf6-5bf40338045b
0x0000, 00000000:61:00.0, 2, GPU-bebdb9f9-f79f-1757-74f8-5e633319af12
0x0000, 00000000:81:00.0, 3, GPU-c3a5fd20-3426-6869-e9cd-d9a80d6cfd0f
0x0000, 00000000:A1:00.0, 4, GPU-df0e69ce-6dbd-a6cf-89f3-92ac8be4e7c6"""


def test_gpu_allocation_manager_assigns_unassigns_and_denies_exhaustion() -> None:
    manager = GpuAllocationManager([0, 1, 2, 3])

    first = manager.assign("container-1", 2)
    second = manager.assign("container-2", 2)
    denied = manager.assign("container-3", 1)

    assert first.assigned_devices == [0, 1]
    assert second.assigned_devices == [2, 3]
    assert not denied.ok
    assert "not enough GPUs" in denied.error_message
    assert manager.get("container-1") == [0, 1]

    manager.unassign("container-1")
    third = manager.assign("container-3", 1)

    assert third.ok
    assert third.assigned_devices == [0]
    assert manager.snapshot() == {"container-2": [2, 3], "container-3": [0]}


def test_dynamic_gpu_allocation_manager_uses_provider_and_releases() -> None:
    provider = _Provider([1, 2])
    manager = DynamicGpuAllocationManager(provider)

    assigned = manager.assign("container-1", 1)
    denied = manager.assign("container-2", 2)

    assert assigned.assigned_devices == [1]
    assert not denied.ok
    assert manager.get("container-1") == [1]
    manager.release_gpu("container-1")
    assert manager.assign("container-2", 2).assigned_devices == [1, 2]


class _Provider:
    def __init__(self, devices: list[int]) -> None:
        self.devices = devices

    def available_devices(self) -> list[int]:
        return list(self.devices)


def _failed_command(
    command: list[str],
    *,
    capture_output: bool,
    text: bool,
    check: bool,
) -> CompletedProcess[str]:
    _ = capture_output, text, check
    return CompletedProcess(args=command, returncode=1, stdout="")

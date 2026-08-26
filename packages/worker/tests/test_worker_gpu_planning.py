from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from worker.events import ContainerRequestContext
from worker.execution import plan_nvidia_mounts
from worker.gpu import (
    DynamicGpuAllocationManager,
    GpuAllocationManager,
    HostNvidiaDriverFileProbe,
    WorkerGpuRuntimeAssigner,
)


@dataclass(frozen=True, slots=True)
class _EmptyDriverProbe:
    """A worker whose NVIDIA runtime injected nothing."""

    def driver_files(self) -> dict[str, str]:
        return {}


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


def test_a_gpu_container_reaches_the_driver_by_the_soname_it_links_against(
    tmp_path: Path,
) -> None:
    """The soname and the versioned file both have to arrive, pointing at one file.

    Nothing linked against `libcuda.so.1`, which is everything CUDA, can start
    from the versioned file alone. The planner once looked for two directories a
    node never had, found neither, and produced no mounts at all: GPU containers
    got their devices, no driver, and a loader error from inside the workload.
    """

    library_dir = tmp_path / "lib"
    library_dir.mkdir()
    (library_dir / "libcuda.so.590.48.01").write_bytes(b"")
    (library_dir / "libcuda.so.1").symlink_to("libcuda.so.590.48.01")
    (library_dir / "libnvidia-ml.so.590.48.01").write_bytes(b"")
    (library_dir / "libc.so.6").write_bytes(b"")
    (library_dir / "libnvidia-dangling.so.1").symlink_to("gone.so")
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    (binary_dir / "nvidia-smi").write_bytes(b"")
    (binary_dir / "bash").write_bytes(b"")

    probe = HostNvidiaDriverFileProbe(
        library_directories=(str(library_dir),),
        binary_directories=(str(binary_dir),),
    )
    mounts = {mount.destination: mount.source for mount in plan_nvidia_mounts(probe.driver_files())}

    real_driver = str(library_dir / "libcuda.so.590.48.01")
    assert mounts["/usr/local/nvidia/lib64/libcuda.so.1"] == real_driver
    assert mounts["/usr/local/nvidia/lib64/libcuda.so.590.48.01"] == real_driver
    assert "/usr/local/nvidia/lib64/libnvidia-ml.so.590.48.01" in mounts
    assert mounts["/usr/local/nvidia/bin/nvidia-smi"] == str(binary_dir / "nvidia-smi")
    # The container's own libc and shell stay the container's.
    assert "/usr/local/nvidia/lib64/libc.so.6" not in mounts
    assert "/usr/local/nvidia/bin/bash" not in mounts
    assert "/usr/local/nvidia/lib64/libnvidia-dangling.so.1" not in mounts


def test_a_worker_without_the_driver_refuses_the_gpu_rather_than_handing_over_devices() -> None:
    """Failing here names the worker; failing later names nothing.

    A container started with devices and no driver reports a CUDA error from
    inside the workload, which reads as the author's bug.
    """

    assigner = WorkerGpuRuntimeAssigner(
        allocation=GpuAllocationManager([0, 1]),
        driver_probe=_EmptyDriverProbe(),
    )

    result = assigner.assign_gpus(ContainerRequestContext(container_id="container-1", gpu_count=1))

    assert not result.ok
    assert "NVIDIA driver libraries" in result.error_message
    assert assigner.allocation.get("container-1") == []

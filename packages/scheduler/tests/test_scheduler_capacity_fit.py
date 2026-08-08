from __future__ import annotations

import pytest
from scheduler.tools import SchedulingRequest, WorkerCapacity
from shared.compute_policy import MachinePool


def _gpu_worker(gpu_type: str) -> WorkerCapacity:
    return WorkerCapacity(
        worker_id="worker-1",
        gpu_type=gpu_type,
        free_cpu=4,
        free_memory_mib=8192,
        free_gpu=1,
        total_cpu=4,
        total_memory_mib=8192,
        total_gpu=1,
    )


def _gpu_request(gpu_type: str) -> SchedulingRequest:
    return SchedulingRequest(id="request-1", cpu=1, memory_mib=512, gpu_type=gpu_type, gpu_count=1)


@pytest.mark.parametrize("requested", ["L4", "l4", "nvidia-l4", "NVIDIA L4"])
def test_a_gpu_worker_takes_every_spelling_its_pool_accepted(requested: str) -> None:
    """Fitting must match the way pool selection matched, or nothing places.

    Pool selection normalises both sides, so a request naming a GPU in any of its
    spellings finds a pool and provisions a machine. Comparing raw strings here
    then refused the worker that machine registered — the documented `gpu="l4"`
    provisioned an instance and never ran on it, and the workload failed on a
    retry limit that named neither the GPU nor the mismatch.
    """
    assert _gpu_worker("L4").can_fit(_gpu_request(requested))


def test_a_gpu_worker_still_refuses_a_different_card() -> None:
    """Normalising must not blur two models into each other."""
    assert not _gpu_worker("L4").can_fit(_gpu_request("A100-80"))


def _private_worker(workspace_id: str) -> WorkerCapacity:
    return WorkerCapacity(
        worker_id="worker-1",
        pool=MachinePool("lazycloud"),
        workspace_id=workspace_id,
        private_worker=True,
        requires_pool_selector=True,
        free_cpu=4,
        free_memory_mib=8192,
        total_cpu=4,
        total_memory_mib=8192,
        total_gpu=0,
    )


def _workspace_request(workspace_id: str) -> SchedulingRequest:
    return SchedulingRequest(
        id="request-1",
        workspace_id=workspace_id,
        pool_selector="lazycloud",
        cpu=1,
        memory_mib=512,
    )


def test_a_private_worker_refuses_another_workspaces_request() -> None:
    """Tenant isolation has to hold where the worker is chosen, not only where it is used.

    Every workspace's default pool carries the same name, so matching on the pool label
    alone put one tenant's request on another tenant's machine. The worker then refused
    it on arrival and it was offered straight back to the same worker, so the request
    never placed, never failed, and reported nothing.
    """
    worker = _private_worker("workspace-a")

    assert not worker.can_fit(_workspace_request("workspace-b"))
    assert worker.fit_rejection(_workspace_request("workspace-b")) == (
        "worker is private to another workspace"
    )
    assert worker.can_fit(_workspace_request("workspace-a"))


def test_a_private_worker_naming_no_workspace_serves_none() -> None:
    """A record written without the authority to name a tenant cannot serve every tenant."""
    assert not _private_worker("").can_fit(_workspace_request("workspace-a"))

from __future__ import annotations

import pytest
from scheduler.tools import SchedulingRequest, WorkerCapacity, select_worker_for_request
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


def _private_worker(owner_user_id: str) -> WorkerCapacity:
    return WorkerCapacity(
        worker_id="worker-1",
        pool=MachinePool("lazycloud"),
        owner_user_id=owner_user_id,
        private_worker=True,
        requires_pool_selector=True,
        free_cpu=4,
        free_memory_mib=8192,
        total_cpu=4,
        total_memory_mib=8192,
        total_gpu=0,
    )


def _account_request(owner_user_id: str) -> SchedulingRequest:
    return SchedulingRequest(
        id="request-1",
        owner_user_id=owner_user_id,
        pool_selector="lazycloud",
        cpu=1,
        memory_mib=512,
    )


def test_a_private_worker_refuses_another_accounts_request() -> None:
    """Tenant isolation has to hold where the worker is chosen, not only where it is used.

    Every workspace's default pool carries the same name, so matching on the pool label
    alone put one tenant's request on another tenant's machine. The worker then refused
    it on arrival and it was offered straight back to the same worker, so the request
    never placed, never failed, and reported nothing.

    The comparison is the owning account, not the workspace: a customer's own machine
    serves every workspace they own, and stops at the boundary of their account.
    """
    worker = _private_worker("account-a")

    assert not worker.can_fit(_account_request("account-b"))
    assert worker.fit_rejection(_account_request("account-b")) == (
        "worker is private to another account"
    )
    assert worker.can_fit(_account_request("account-a"))


def test_the_shared_fleet_serves_an_account_it_does_not_belong_to() -> None:
    """The shared fleet is what an unconfigured workspace lands on, so it serves anyone.

    Its counterpart above refuses a foreign account. The pair is the whole rule: a
    machine somebody connected is theirs, and platform capacity is not. Asserted with
    a mismatched owner because equal owners would pass either way and prove nothing.
    """
    shared = WorkerCapacity(
        worker_id="worker-1",
        pool=MachinePool("lazycloud"),
        owner_user_id="account-a",
        private_worker=False,
        free_cpu=4,
        free_memory_mib=8192,
        total_cpu=4,
        total_memory_mib=8192,
        total_gpu=0,
    )

    assert shared.can_fit(_account_request("account-b"))
    assert shared.fit_rejection(_account_request("account-b")) == ""


def test_a_private_worker_naming_no_account_serves_none() -> None:
    """A record written without the authority to name a tenant cannot serve every tenant."""
    assert not _private_worker("").can_fit(_account_request("account-a"))


def test_work_packs_onto_the_fullest_worker_that_fits() -> None:
    """Placement is what decides whether a pool can ever shrink.

    Sending each request to the emptiest worker keeps every machine warm, so
    none of them goes idle long enough to be released and the pool holds a
    floor of machines no amount of quiet removes.
    """

    busy = WorkerCapacity(
        worker_id="busy",
        pool=MachinePool("lazycloud"),
        total_cpu=4,
        free_cpu=2,
        total_memory_mib=8192,
        free_memory_mib=4096,
        total_gpu=0,
        free_gpu=0,
    )
    empty = WorkerCapacity(
        worker_id="empty",
        pool=MachinePool("lazycloud"),
        total_cpu=4,
        free_cpu=4,
        total_memory_mib=8192,
        free_memory_mib=8192,
        total_gpu=0,
        free_gpu=0,
    )
    request = SchedulingRequest(id="c-1", cpu=1, memory_mib=1024)

    chosen = select_worker_for_request(request, [empty, busy])

    assert chosen is not None
    assert chosen.worker_id == "busy"

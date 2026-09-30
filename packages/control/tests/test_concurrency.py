from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from control.service import ControlServices
from database.context import ServiceContext


def test_lowering_limit_keeps_outstanding_acquisitions(
    committed_service_context: ServiceContext,
) -> None:
    control = ControlServices.create(committed_service_context)
    limit = control.concurrency.upsert_concurrency_limit("requests", limit=3)
    for _ in range(3):
        assert control.concurrency.acquire_concurrency(limit.id).acquired
    lowered = control.concurrency.upsert_concurrency_limit("requests", limit=1)
    assert lowered.in_flight == 3
    assert not control.concurrency.acquire_concurrency(limit.id).acquired
    for remaining in (2, 1, 0):
        assert control.concurrency.release_concurrency(limit.id).record.in_flight == remaining
        if remaining:
            assert not control.concurrency.acquire_concurrency(limit.id).acquired
    assert control.concurrency.acquire_concurrency(limit.id).acquired


def test_concurrent_acquisition_never_exceeds_limit(
    committed_service_context: ServiceContext,
) -> None:
    control = ControlServices.create(committed_service_context)
    limit = control.concurrency.upsert_concurrency_limit("requests", limit=1)
    start = Barrier(8)

    def acquire(_index: int) -> bool:
        start.wait(timeout=10)
        return control.concurrency.acquire_concurrency(limit.id).acquired

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(acquire, range(8)))
    assert sum(results) == 1
    assert control.concurrency.current_concurrency_limit().in_flight == 1
    assert control.concurrency.release_concurrency(limit.id).record.in_flight == 0

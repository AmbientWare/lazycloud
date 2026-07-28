"""Prove authenticated drain and replacement for two explicitly named test workers."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter
from shared.http.compute import WorkerDrainResponse, WorkerListResponse, WorkerResponse
from shared.scheduling import SchedulerWorkerStatus

BLOCKED = 77
_WORKER = TypeAdapter(WorkerResponse)
_AVAILABLE = SchedulerWorkerStatus.Available.value
_DRAINED = SchedulerWorkerStatus.Unavailable.value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--replacement-worker-id", required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("worker drain acceptance requires --live")
    if args.worker_id == args.replacement_worker_id:
        raise RuntimeError("the replacement worker must have a distinct identity")
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").rstrip("/")
    token = os.getenv("LAZYCLOUD_E2E_ADMIN_TOKEN", "")
    if not endpoint or not token:
        return _blocked("LAZYCLOUD_ENDPOINT and LAZYCLOUD_E2E_ADMIN_TOKEN are required")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        httpx.get(f"{endpoint}/health", timeout=5).raise_for_status()
        workers = _workers(endpoint, headers)
    except httpx.HTTPError as exc:
        return _blocked(f"prepared control plane is unavailable: {exc}")
    source = _exact_worker(workers, args.worker_id)
    if source.status != _AVAILABLE:
        return _blocked(f"worker {source.id} is not {_AVAILABLE}")
    if source.active_containers:
        return _blocked(f"worker {source.id} owns active containers and will not be drained")

    primary_error: BaseException | None = None
    evidence: dict[str, object] = {}
    drained = False
    try:
        response = httpx.post(
            f"{endpoint}/api/v1/workers/{quote(source.id, safe='')}/drain",
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        result = WorkerDrainResponse.model_validate(response.json())
        drained = True
        if result.worker.status != _DRAINED or result.stopped_container_ids:
            raise RuntimeError("public drain returned an unsafe or unexpected outcome")
        replacement = _await_available_worker(
            endpoint,
            headers,
            args.replacement_worker_id,
        )
        evidence = {
            "drained_worker_id": result.worker.id,
            "replacement_worker_id": replacement.id,
            "replacement_status": replacement.status,
        }
    except BaseException as exc:
        primary_error = exc

    cleanup_error = ""
    if drained:
        try:
            response = httpx.post(
                f"{endpoint}/api/v1/workers/{quote(source.id, safe='')}/uncordon",
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            restored = _WORKER.validate_python(response.json())
            if restored.status != _AVAILABLE:
                cleanup_error = f"worker {source.id} did not return to {_AVAILABLE}"
        except Exception as exc:
            cleanup_error = str(exc)
    if primary_error is not None:
        if cleanup_error:
            raise RuntimeError(
                f"worker drain failed and uncordon cleanup failed: {cleanup_error}"
            ) from primary_error
        raise primary_error
    if cleanup_error:
        raise RuntimeError(f"worker uncordon cleanup failed: {cleanup_error}")
    print(
        json.dumps(
            {
                "accepted": True,
                "evidence": evidence,
                "cleanup": f"worker {source.id} returned to {_AVAILABLE}",
            },
            sort_keys=True,
        )
    )
    return 0


def _workers(endpoint: str, headers: dict[str, str]) -> list[WorkerResponse]:
    response = httpx.get(
        f"{endpoint}/api/v1/workers",
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    return WorkerListResponse.model_validate(response.json()).workers


def _exact_worker(workers: list[WorkerResponse], worker_id: str) -> WorkerResponse:
    matches = [worker for worker in workers if worker.id == worker_id]
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one worker named {worker_id}")
    return matches[0]


def _await_available_worker(
    endpoint: str,
    headers: dict[str, str],
    worker_id: str,
) -> WorkerResponse:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        matches = [worker for worker in _workers(endpoint, headers) if worker.id == worker_id]
        if len(matches) == 1 and matches[0].status == _AVAILABLE:
            return matches[0]
        time.sleep(0.5)
    raise RuntimeError(f"replacement worker {worker_id} did not become {_AVAILABLE}")


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

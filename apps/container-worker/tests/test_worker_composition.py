from __future__ import annotations

from collections.abc import Iterable, Mapping
from concurrent.futures import Future
from pathlib import Path
from typing import IO

import httpx
import pytest
from container_worker_app import composition
from container_worker_app.composition import build_worker_process_services
from container_worker_app.settings import WorkerSettings
from networking.internal_http import InternalHttpClient, InternalHttpConnectError
from worker.configuration import WorkerConfiguration, WorkerPathConfiguration
from worker.image_runtime import ImageRuntimeClient, ImageRuntimeResponse
from worker.oci_runtime import OciRuntimeSpecBuilder
from worker.repository_client import build_worker_repository_http_client
from worker.repository_errors import WorkerRepositoryClientError
from worker.runtime_config import OciRuntimeName, RuntimeBinaryConfig

from worker import repository_client

_CAPACITY_OWNER_ID = "839fc92e-c26d-4e31-84c1-a827c2768607"


def test_worker_requires_repository_credentials_before_registration(
    tmp_path: Path,
) -> None:
    with pytest.raises(WorkerRepositoryClientError, match="worker repository token is required"):
        build_worker_process_services(
            settings=WorkerSettings(
                worker_id="worker-1",
                capacity_owner_id=_CAPACITY_OWNER_ID,
                configuration=WorkerConfiguration(
                    paths=WorkerPathConfiguration(
                        bundle_root=tmp_path / "bundles",
                        image_cache_path=str(tmp_path / "image-cache"),
                        image_mount_root=str(tmp_path / "image-mounts"),
                        checkpoint_root=str(tmp_path / "checkpoints"),
                        image_build_root=tmp_path / "builds",
                    )
                ),
            ),
            runtime_configs={OciRuntimeName.Runc: RuntimeBinaryConfig()},
        )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class _HeldAgent(InternalHttpClient):
    """The node agent while a reserve is held: every call is refused, and recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | Iterable[bytes] | IO[bytes] | None = None,
        timeout_seconds: float | None = None,
    ) -> httpx.Response:
        self.calls.append(url)
        raise InternalHttpConnectError(f"{method} refused")


class _ImageRuntime(ImageRuntimeClient):
    def health(self) -> ImageRuntimeResponse:
        return ImageRuntimeResponse(id="health", ok=True)


class _SpecBuilder(OciRuntimeSpecBuilder):
    def prepare_managed_runtimes(self) -> None:
        return None


def test_readiness_preparation_finishes_while_the_control_plane_refuses_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held reserve worker prepares with the control plane unreachable, and so can say it waits.

    The host network needs the control plane's lock, so it runs after admission.
    """
    monkeypatch.setattr(repository_client, "time", _Clock())
    agent = _HeldAgent()
    client = build_worker_repository_http_client(
        endpoint="http://agent.invalid",
        token="worker-token",
        admission_hold_seconds=1800.0,
        http=agent,
    )
    settings = WorkerSettings(
        worker_id="worker-1",
        capacity_owner_id=_CAPACITY_OWNER_ID,
        worker_token="worker-token",
    )
    readiness: Future[None] = Future()
    prepare, validate = composition._readiness_steps(
        _ImageRuntime(),
        spec_builder=_SpecBuilder(bundle_root=tmp_path),
        network_backend=composition._client_network_backend(settings, client),
        readiness=readiness,
        gpu_count=0,
        gpu_devices="",
    )

    prepare()

    assert readiness.done() and readiness.exception() is None
    assert agent.calls == []
    with pytest.raises(WorkerRepositoryClientError, match="did not admit this reserve worker"):
        validate()
    assert agent.calls

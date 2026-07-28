from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lazycloud.abstractions.serve import (
    ContainerWorkspaceSyncer,
    ServePreviewSession,
    resolve_serve_url,
)
from lazycloud.terminal import Terminal
from shared.http.compute import ContainerResponse
from shared.http.gateway import (
    AttachToContainerResponse,
    ContainerWorkspaceSyncOperation,
    GetUrlRequest,
    GetUrlResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from tests.fakes import http_api_error


@dataclass
class InterruptingGatewayClient:
    urls: list[GetUrlRequest] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    sync_requests: list[SyncContainerWorkspaceBody] = field(default_factory=list)

    def get_url(self, request: GetUrlRequest) -> GetUrlResponse:
        self.urls.append(request)
        return GetUrlResponse(url=f"{request.external_url}/endpoint/id/{request.stub_id}")

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = container_id
        _ = poll_interval_seconds
        raise KeyboardInterrupt

    def stop_container(self, container_id: str) -> ContainerResponse:
        self.stopped.append(container_id)
        return ContainerResponse(
            id=container_id,
            name="preview",
            image="python:3.12",
            command=[],
            workspace_id="default",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        self.sync_requests.append(body)
        return SyncContainerWorkspaceResponse(path=body.path)


@dataclass
class InitiallyUnpublishedGatewayClient(InterruptingGatewayClient):
    failures_remaining: int = 1

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        self.sync_requests.append(body)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise http_api_error(
                "worker address not published for container ctr-serve",
                status_code=503,
            )
        return SyncContainerWorkspaceResponse(path=body.path)


@dataclass
class InitiallyMissingContainerGatewayClient(InterruptingGatewayClient):
    failures_remaining: int = 1

    def sync_container_workspace(
        self,
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        self.sync_requests.append(body)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise http_api_error("Container not found: ctr-serve", status_code=404)
        return SyncContainerWorkspaceResponse(path=body.path)


@dataclass
class TimingOutAttachGatewayClient(InterruptingGatewayClient):
    timeouts_remaining: int = 1

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = container_id
        _ = poll_interval_seconds
        if self.timeouts_remaining > 0:
            self.timeouts_remaining -= 1
            raise TimeoutError("timed out")
        yield AttachToContainerResponse(output="", done=True)


@dataclass
class FailingAttachGatewayClient(InterruptingGatewayClient):
    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = container_id
        _ = poll_interval_seconds
        raise RuntimeError("attach failed")


def test_serve_preview_resolves_stub_url_and_stops_container_on_interrupt() -> None:
    gateway = InterruptingGatewayClient()

    url = resolve_serve_url(
        gateway,
        stub_id="stub-endpoint",
        url_type="host",
        external_url="https://example.test",
    )
    session = ServePreviewSession(
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url=url.url,
        gateway_client=gateway,
        resource_client=gateway,
        terminal=Terminal(quiet=True),
        sync_dir=None,
    )

    session.run()

    assert url.url == "https://example.test/endpoint/id/stub-endpoint"
    assert gateway.urls[0].mode == "host"
    assert gateway.stopped == ["ctr-serve"]


def test_serve_preview_retries_attach_timeout() -> None:
    gateway = TimingOutAttachGatewayClient()
    session = ServePreviewSession(
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://example.test/endpoint/id/stub-endpoint",
        gateway_client=gateway,
        resource_client=gateway,
        terminal=Terminal(quiet=True),
        sync_dir=None,
    )

    session.run()

    assert gateway.stopped == []
    assert gateway.timeouts_remaining == 0


def test_serve_preview_stops_container_when_attach_fails() -> None:
    gateway = FailingAttachGatewayClient()
    session = ServePreviewSession(
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://example.test/endpoint/id/stub-endpoint",
        gateway_client=gateway,
        resource_client=gateway,
        terminal=Terminal(quiet=True),
        sync_dir=None,
    )

    try:
        session.run()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected attach failure")

    assert gateway.stopped == ["ctr-serve"]


def test_serve_workspace_syncer_writes_filtered_tree_through_gateway(tmp_path: Path) -> None:
    gateway = InterruptingGatewayClient()
    source = tmp_path / "source"
    (source / "pkg").mkdir(parents=True)
    (source / "__pycache__").mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "pkg" / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "ignored.py").write_text("raise RuntimeError\n", encoding="utf-8")
    (source / "__pycache__" / "skip.py").write_text("skip\n", encoding="utf-8")
    (source / ".lazycloudignore").write_text(
        ".lazycloudignore\nignored.py\n__pycache__\n",
        encoding="utf-8",
    )

    ContainerWorkspaceSyncer(
        container_id="ctr-serve",
        local_dir=str(source),
        gateway_client=gateway,
        terminal=Terminal(quiet=True),
    ).sync_once()

    writes = {request.path: request for request in gateway.sync_requests}
    assert writes["app.py"].operation is ContainerWorkspaceSyncOperation.Write
    assert writes["app.py"].data == b"print('ok')\n"
    assert writes["pkg/worker.py"].data == b"VALUE = 1\n"
    assert "ignored.py" not in writes
    assert "__pycache__/skip.py" not in writes


def test_serve_workspace_syncer_can_seed_from_source_package_before_deltas(
    tmp_path: Path,
) -> None:
    gateway = InterruptingGatewayClient()
    source = tmp_path / "source"
    source.mkdir()
    app = source / "app.py"
    app.write_text("print('one')\n", encoding="utf-8")
    syncer = ContainerWorkspaceSyncer(
        container_id="ctr-serve",
        local_dir=str(source),
        gateway_client=gateway,
        terminal=Terminal(quiet=True),
        full_initial_sync=False,
    )

    syncer._sync_initial_with_retries()
    app.write_text("print('two')\n", encoding="utf-8")
    syncer._sync_delta(syncer._snapshot | {"app.py": type(syncer._snapshot["app.py"])(13, 0)})

    assert len(gateway.sync_requests) == 1
    assert gateway.sync_requests[0].path == "app.py"
    assert gateway.sync_requests[0].operation is ContainerWorkspaceSyncOperation.Write


def test_serve_workspace_syncer_retries_until_worker_address_is_published(
    tmp_path: Path,
) -> None:
    gateway = InitiallyUnpublishedGatewayClient()
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")

    syncer = ContainerWorkspaceSyncer(
        container_id="ctr-serve",
        local_dir=str(source),
        gateway_client=gateway,
        terminal=Terminal(quiet=True),
        poll_seconds=0,
        initial_sync_timeout_seconds=1,
    )

    syncer._sync_initial_with_retries()

    assert len(gateway.sync_requests) == 2
    assert gateway.failures_remaining == 0


def test_serve_workspace_syncer_retries_until_container_service_is_ready(
    tmp_path: Path,
) -> None:
    gateway = InitiallyMissingContainerGatewayClient()
    source = tmp_path / "source"
    source.mkdir()
    (source / "app.py").write_text("print('ok')\n", encoding="utf-8")
    syncer = ContainerWorkspaceSyncer(
        container_id="ctr-serve",
        local_dir=str(source),
        gateway_client=gateway,
        terminal=Terminal(quiet=True),
        poll_seconds=0,
        initial_sync_timeout_seconds=1,
    )

    syncer._sync_initial_with_retries()

    assert len(gateway.sync_requests) == 2
    assert gateway.failures_remaining == 0

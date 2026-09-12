from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from lazycloud.abstractions.serve import (
    ContainerWorkspaceSyncer,
    ServePreviewSession,
    write_serve_preview,
)
from lazycloud.session.source_sync import SourcePackageSyncer
from lazycloud.terminal import Terminal
from shared.deployments import DeploymentKind
from shared.http.gateway import (
    AttachToContainerResponse,
    ContainerWorkspaceSyncOperation,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from shared.http.previews import PreviewSessionResponse, PreviewSessionStatus
from shared.paths import HOME_ENV
from tests.fakes import FakeUploadClient, http_api_error


@dataclass
class InterruptingGatewayClient:
    stopped: list[str] = field(default_factory=list)
    sync_requests: list[SyncContainerWorkspaceBody] = field(default_factory=list)

    def create(self, stub_id: str, *, timeout: int = 0) -> PreviewSessionResponse:
        return self.get_preview("preview-1")

    def get_preview(self, preview_id: str) -> PreviewSessionResponse:
        return PreviewSessionResponse(
            id=preview_id,
            workspace_id="default",
            source_stub_id="stub-source",
            execution_stub_id="stub-endpoint",
            container_id="ctr-serve",
            status=PreviewSessionStatus.Stopped
            if preview_id in self.stopped
            else PreviewSessionStatus.Active,
            public=False,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=None,
        )

    def renew(self, preview_id: str) -> PreviewSessionResponse:
        return self.get_preview(preview_id)

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = container_id
        _ = poll_interval_seconds
        raise KeyboardInterrupt

    def stop(self, preview_id: str) -> None:
        self.stopped.append(preview_id)

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


def test_serve_preview_stops_session_on_interrupt() -> None:
    gateway = InterruptingGatewayClient()

    session = ServePreviewSession(
        preview_id="preview-1",
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://example.test/api/v1/previews/preview-1/invoke",
        gateway_client=gateway,
        preview_client=gateway,
        terminal=Terminal(quiet=True),
    )

    session.run()

    assert gateway.stopped == ["preview-1"]


def test_serve_preview_retries_attach_timeout() -> None:
    gateway = TimingOutAttachGatewayClient()
    session = ServePreviewSession(
        preview_id="preview-1",
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://example.test/endpoint/id/stub-endpoint",
        gateway_client=gateway,
        preview_client=gateway,
        terminal=Terminal(quiet=True),
        attach_poll_seconds=0,
    )

    session.run()

    assert gateway.stopped == ["preview-1"]
    assert gateway.timeouts_remaining == 0


def test_serve_preview_stops_container_when_attach_fails() -> None:
    gateway = FailingAttachGatewayClient()
    session = ServePreviewSession(
        preview_id="preview-1",
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://example.test/endpoint/id/stub-endpoint",
        gateway_client=gateway,
        preview_client=gateway,
        terminal=Terminal(quiet=True),
    )

    try:
        session.run()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected attach failure")

    assert gateway.stopped == ["preview-1"]


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


def test_initial_sync_failure_stops_only_its_preview_and_removes_its_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv(HOME_ENV, str(state))
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "app.py").write_text("VALUE = 1\n")
    source = SourcePackageSyncer(
        FakeUploadClient(object_id="obj-source"), root_dir=source_root
    ).sync()
    baseline = write_serve_preview(
        kind=DeploymentKind.Endpoint,
        name="app",
        app="test",
        workspace="default",
        endpoint="https://example.test",
        preview_id="baseline",
        stub_id="stub-baseline",
        container_id="ctr-baseline",
        url="https://example.test/baseline",
    )
    record = write_serve_preview(
        kind=DeploymentKind.Endpoint,
        name="app",
        app="test",
        workspace="default",
        endpoint="https://example.test",
        preview_id="preview-1",
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://example.test/preview-1",
    )
    gateway = InitiallyUnpublishedGatewayClient()
    session = ServePreviewSession(
        preview_id=record.preview_id,
        stub_id=record.stub_id,
        container_id=record.container_id,
        url=record.url,
        gateway_client=gateway,
        preview_client=gateway,
        source=source,
        terminal=Terminal(quiet=True),
        preview_record=record,
        initial_sync_timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="worker address not published"):
        session.run()

    assert gateway.stopped == [record.preview_id]
    assert {path.name for path in state.rglob("*.json")} == {f"{baseline.preview_id}.json"}


def test_serve_workspace_syncer_reconciles_uploaded_archive_with_original_prefix(
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
        archive_prefix=("pkg",),
        seed_files=("pkg/app.py", "pkg/deleted.py"),
    )

    app.write_text("print('two')\n", encoding="utf-8")
    syncer.sync_once()

    operations = {request.path: request for request in gateway.sync_requests}
    assert operations["pkg/deleted.py"].operation is ContainerWorkspaceSyncOperation.Delete
    assert operations["pkg/app.py"].data == b"print('two')\n"
    assert operations["pkg/app.py"].operation is ContainerWorkspaceSyncOperation.Write


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

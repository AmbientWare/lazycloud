from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lazycloud.abstractions.serve import (
    ContainerWorkspaceSyncer,
    ServePreviewSession,
)
from lazycloud.terminal import Terminal
from shared.http.compute import ContainerResponse
from shared.http.gateway import (
    AttachToContainerResponse,
)
from shared.http.workspace_sync import (
    WorkspaceSyncBatch,
    WorkspaceSyncOperation,
    WorkspaceSyncResponse,
)


@dataclass
class InterruptingGatewayClient:
    stopped: list[str] = field(default_factory=list)
    sync_requests: list[WorkspaceSyncBatch] = field(default_factory=list)

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
        body: WorkspaceSyncBatch,
    ) -> WorkspaceSyncResponse:
        self.sync_requests.append(body.model_copy(update={"data": tuple(body.data)}))
        return WorkspaceSyncResponse(applied=len(body.manifest.entries))


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


def test_serve_preview_stops_container_on_interrupt() -> None:
    gateway = InterruptingGatewayClient()
    session = ServePreviewSession(
        stub_id="stub-endpoint",
        container_id="ctr-serve",
        url="https://ctr-serve.example.test",
        gateway_client=gateway,
        resource_client=gateway,
        terminal=Terminal(quiet=True),
        sync_dir=None,
    )

    session.run()

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
        attach_poll_seconds=0,
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

    writes = {
        entry.path: entry for request in gateway.sync_requests for entry in request.manifest.entries
    }
    assert writes["app.py"].operation is WorkspaceSyncOperation.Write
    assert b"".join(gateway.sync_requests[0].data) == b"print('ok')\nVALUE = 1\n"
    assert "ignored.py" not in writes
    assert "__pycache__/skip.py" not in writes

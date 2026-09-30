from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from lazycloud.abstractions.pod import Container
from lazycloud.exceptions import UnsupportedFeatureError
from shared.http.compute import ContainerResponse
from shared.http.gateway import (
    AttachToContainerResponse,
)
from shared.http.workspace_sync import WorkspaceSyncBatch, WorkspaceSyncResponse

from lazycloud import App, Image


@dataclass
class FakeGatewayClient:
    requests: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    sync_requests: list[WorkspaceSyncBatch] = field(default_factory=list)
    fail: bool = False

    def stop_container(self, container_id: str) -> ContainerResponse:
        self.requests.append(container_id)
        return ContainerResponse(
            id=container_id,
            name="worker",
            image="python:3.12",
            command=[],
            workspace_id="default",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def attach_to_container_events(
        self,
        container_id: str,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> Iterator[AttachToContainerResponse]:
        _ = poll_interval_seconds
        self.attached.append(container_id)
        yield AttachToContainerResponse(output="attached\n")
        yield AttachToContainerResponse(output="complete\n")
        yield AttachToContainerResponse(done=True, exit_code=0)

    def sync_container_workspace(
        self,
        body: WorkspaceSyncBatch,
    ) -> WorkspaceSyncResponse:
        self.sync_requests.append(body.model_copy(update={"data": tuple(body.data)}))
        return WorkspaceSyncResponse(applied=len(body.manifest.entries))


@pytest.mark.parametrize("method", ["deploy", "create", "run", "shell"])
def test_pod_operations_name_the_unsupported_resource(method: str) -> None:
    devbox = App("billing").devbox(
        name="workbench", image=Image(), disk="10Gi", cpu=1, memory="1Gi"
    )

    with pytest.raises(UnsupportedFeatureError, match=r"devbox workbench.*devboxes"):
        getattr(devbox, method)()


def test_container_attaches_to_existing_container(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gateway = FakeGatewayClient()
    container = Container(container_id="ctr-existing", client=gateway)

    response = container.attach()

    assert response.done is True
    assert response.output == "attached\ncomplete\n"
    assert gateway.attached == ["ctr-existing"]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "attached\ncomplete\n"

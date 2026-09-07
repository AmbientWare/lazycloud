from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

import pytest
from lazycloud.abstractions.pod import Container, Pod, PodOperationError
from shared.deployments import DeploymentKind
from shared.http.compute import ContainerResponse
from shared.http.deployments import DeploymentListResponse, DeploymentResponse
from shared.http.gateway import (
    AttachToContainerResponse,
    SyncContainerWorkspaceBody,
    SyncContainerWorkspaceResponse,
)
from tests.fakes import FakeDeploymentClient

T = TypeVar("T")


@dataclass
class FakeGatewayClient:
    requests: list[str] = field(default_factory=list)
    attached: list[str] = field(default_factory=list)
    sync_requests: list[SyncContainerWorkspaceBody] = field(default_factory=list)
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
        body: SyncContainerWorkspaceBody,
    ) -> SyncContainerWorkspaceResponse:
        self.sync_requests.append(body)
        return SyncContainerWorkspaceResponse(path=body.path)


@dataclass
class FakePodDeploymentClient(FakeDeploymentClient):
    stopped: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    scaled: list[tuple[str, int]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def _response(self, deployment_id: str) -> DeploymentResponse:
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        return DeploymentResponse(
            id=deployment_id,
            name="worker",
            kind=DeploymentKind.Pod,
            stub_id=self.stub_id,
            created_at=created_at,
            updated_at=created_at,
        )

    def list_deployments(
        self,
        *,
        active: bool | None = None,
        app_id: str | None = None,
        name: str | None = None,
        latest: bool = False,
        limit: int = 100,
        cursor: str | None = None,
    ) -> DeploymentListResponse:
        del active, app_id, latest, limit, cursor
        deployment_id = self.deployment_id
        if deployment_id is None:
            raise AssertionError("fake pod deployment client has no configured deployment ID")
        response = self._response(deployment_id)
        return DeploymentListResponse(data=[response] if name in {None, response.name} else [])

    def deployment(self, deployment_id: str) -> DeploymentResponse:
        return self._response(deployment_id)

    def stop_deployment(self, deployment_id: str) -> DeploymentResponse:
        self.stopped.append(deployment_id)
        return self._response(deployment_id)

    def start_deployment(self, deployment_id: str) -> DeploymentResponse:
        self.started.append(deployment_id)
        return self._response(deployment_id)

    def scale_deployment(self, deployment_id: str, replicas: int) -> DeploymentResponse:
        self.scaled.append((deployment_id, replicas))
        return self._response(deployment_id)

    def delete_deployment(self, deployment_id: str) -> None:
        self.deleted.append(deployment_id)


def _bind_internal_state(resource: T, /, **values: object) -> T:
    for name, value in values.items():
        setattr(resource, name, value)
    return resource


def test_pod_lifecycle_resolution_raises_typed_error() -> None:
    pod = Pod(_app_slug="billing", name="worker")
    _bind_internal_state(
        pod,
        deployment_client=FakePodDeploymentClient(fail_resolve=True),
        deployment_resource_client=FakePodDeploymentClient(),
    )

    with pytest.raises(PodOperationError, match="target not found"):
        pod.pause(version=4)


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

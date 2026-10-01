"""The compute operations of the public API: capacity, the AWS connection and machines."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from shared.api import (
    AwsConnection,
    AwsConnectionAuthorization,
    AwsConnectionEnvelope,
    AwsConnectionRequest,
    AwsReconnectRequest,
    ComputeInstance,
    ComputeInstancePage,
    ComputeSummary,
    ComputeWorkload,
    ComputeWorkloadPage,
    Machine,
    MachineJoinCommand,
    MachineJoinRequest,
    MachinePage,
    MachineUpdate,
)

from lazycloud.clients.api import ApiClient, _path

ItemT = TypeVar("ItemT")

# The largest page the compute collections serve.
_PAGE_LIMIT = 100


@dataclass
class ComputeApi:
    api: ApiClient

    def summary(self, workspace: str) -> ComputeSummary:
        return self.api._send(
            ComputeSummary, "GET", _path("v1", "workspaces", workspace, "compute")
        )

    def workloads(self, workspace: str) -> list[ComputeWorkload]:
        path = _path("v1", "workspaces", workspace, "compute", "workloads")

        def page(params: dict[str, str | int]) -> tuple[list[ComputeWorkload], str | None]:
            result = self.api._send(ComputeWorkloadPage, "GET", path, params=params)
            return result.workloads, result.next_cursor

        return _all_pages(page)

    def machines(self, workspace: str) -> list[Machine]:
        """Joined machines that serve the workspace."""
        path = _path("v1", "workspaces", workspace, "machines")

        def page(params: dict[str, str | int]) -> tuple[list[Machine], str | None]:
            result = self.api._send(MachinePage, "GET", path, params=params)
            return result.machines, result.next_cursor

        return _all_pages(page)

    def instances(self) -> list[ComputeInstance]:
        """Instances in the account's connected AWS account, newest first."""

        def page(params: dict[str, str | int]) -> tuple[list[ComputeInstance], str | None]:
            result = self.api._send(
                ComputeInstancePage, "GET", "/v1/compute/instances", params=params
            )
            return result.instances, result.next_cursor

        return _all_pages(page)

    def aws_connection(self) -> AwsConnection | None:
        return self.api._send(AwsConnectionEnvelope, "GET", "/v1/aws-connection").connection

    def connect_aws(self, request: AwsConnectionRequest) -> AwsConnectionAuthorization:
        return self.api._send(
            AwsConnectionAuthorization, "POST", "/v1/aws-connection", body=request
        )

    def disconnect_aws(self) -> AwsConnection | None:
        """The connection while removal runs, or None once it is gone."""
        return self.api._send(AwsConnectionEnvelope, "DELETE", "/v1/aws-connection").connection

    def validate_aws(self) -> AwsConnection:
        return self.api._send(AwsConnection, "POST", "/v1/aws-connection/validate")

    def reconnect_aws(self, request: AwsReconnectRequest) -> AwsConnectionAuthorization:
        return self.api._send(
            AwsConnectionAuthorization, "POST", "/v1/aws-connection/reconnect", body=request
        )

    def cancel_aws_reconnect(self) -> AwsConnection:
        return self.api._send(AwsConnection, "DELETE", "/v1/aws-connection/reconnect")

    def retry_aws(self) -> AwsConnection:
        return self.api._send(AwsConnection, "POST", "/v1/aws-connection/retry")

    def join_command(self, request: MachineJoinRequest) -> MachineJoinCommand:
        return self.api._send(MachineJoinCommand, "POST", "/v1/machines/join-command", body=request)

    def update_machine(self, machine: str, update: MachineUpdate) -> Machine:
        """`machine` is a name or an id."""
        return self.api._send(Machine, "PATCH", _path("v1", "machines", machine), body=update)

    def remove_machine(self, machine: str) -> None:
        self.api._send(None, "DELETE", _path("v1", "machines", machine))


def _all_pages(
    fetch: Callable[[dict[str, str | int]], tuple[list[ItemT], str | None]],
) -> list[ItemT]:
    items: list[ItemT] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": _PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        page, cursor = fetch(params)
        items.extend(page)
        if not cursor:
            return items


__all__ = ["ComputeApi"]

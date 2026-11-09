"""The compute operations of the public API: capacity, the AWS connection and machines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel

from lazycloud.clients.api import ApiClient, _path, pages
from lazycloud.contracts.api import (
    AwsConnection,
    AwsConnectionAuthorization,
    AwsConnectionEnvelope,
    AwsConnectionRequest,
    AwsForgetResult,
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

PageT = TypeVar("PageT", bound=BaseModel)

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
        return [
            item
            for page in pages(lambda cursor: self._page(ComputeWorkloadPage, path, cursor))
            for item in page.workloads
        ]

    def machines(self, workspace: str) -> list[Machine]:
        """Joined machines that serve the workspace."""
        path = _path("v1", "workspaces", workspace, "machines")
        return [
            item
            for page in pages(lambda cursor: self._page(MachinePage, path, cursor))
            for item in page.machines
        ]

    def instances(self) -> list[ComputeInstance]:
        """Instances in the account's connected AWS account, newest first."""
        return [
            item
            for page in pages(
                lambda cursor: self._page(ComputeInstancePage, "/v1/compute/instances", cursor)
            )
            for item in page.instances
        ]

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

    def forget_aws(self) -> AwsForgetResult:
        """Removes a connection in action_required, returning what stays in AWS."""
        return self.api._send(AwsForgetResult, "POST", "/v1/aws-connection/forget")

    def join_command(self, request: MachineJoinRequest) -> MachineJoinCommand:
        return self.api._send(MachineJoinCommand, "POST", "/v1/machines/join-command", body=request)

    def update_machine(self, machine: str, update: MachineUpdate) -> Machine:
        """`machine` is a name or an id."""
        return self.api._send(Machine, "PATCH", _path("v1", "machines", machine), body=update)

    def remove_machine(self, machine: str) -> None:
        self.api._send(None, "DELETE", _path("v1", "machines", machine))

    def _page(self, model: type[PageT], path: str, cursor: str | None) -> PageT:
        params: dict[str, str | int] = {"limit": _PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        return self.api._send(model, "GET", path, params=params)


__all__ = ["ComputeApi"]

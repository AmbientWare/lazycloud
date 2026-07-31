from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from pydantic import JsonValue
from shared.http.aws_connections import (
    AwsConnectionAuthorizationResponse,
    AwsConnectionCreateRequest,
    AwsConnectionCurrentResponse,
    AwsConnectionReconnectRequest,
    AwsConnectionResponse,
)
from shared.http.compute import (
    MachineJoinCommandRequest,
    MachineJoinCommandResponse,
    PoolCapacityExtendRequest,
    PoolCapacityLaunchRequest,
    PoolCapacityResponse,
    PoolJoinCommandRequest,
    PoolJoinCommandResponse,
    PoolJoinTokenRequest,
    PoolJoinTokenResponse,
    PoolMachineListResponse,
    PoolOfferListResponse,
    PoolOfferQuery,
    PoolScaleRequest,
    PoolScaleResponse,
    WorkerDrainResponse,
    WorkerResponse,
)
from shared.http.compute_policy import (
    ComputeCatalogResponse,
    WorkspaceComputeInstanceListResponse,
    WorkspaceComputePolicyPatchRequest,
    WorkspaceComputePolicyResponse,
    WorkspaceComputePolicyUpdateRequest,
    WorkspaceComputeSummaryResponse,
    WorkspaceComputeWorkloadListResponse,
)
from shared.http_transport import HttpChannel


class ComputeControlChannel(Protocol):
    def get(self, path: str) -> JsonValue: ...

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue: ...

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue: ...


@dataclass(slots=True)
class ComputeClient:
    channel: ComputeControlChannel
    workspace: str = "default"

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        timeout_seconds: float = 10.0,
        workspace: str = "default",
    ) -> ComputeClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def current_connection(self) -> AwsConnectionResponse | None:
        response = AwsConnectionCurrentResponse.model_validate(self.channel.get(self._aws_path("")))
        return response.connection

    def connect_account(
        self,
        *,
        account_id: str,
        role_arn: str | None = None,
    ) -> AwsConnectionAuthorizationResponse:
        request = AwsConnectionCreateRequest(account_id=account_id, role_arn=role_arn)
        return AwsConnectionAuthorizationResponse.model_validate(
            self.channel.post(self._aws_path(""), request.model_dump(mode="json"))
        )

    def validate_connection(self) -> AwsConnectionResponse:
        return AwsConnectionResponse.model_validate(self.channel.post(self._aws_path("/validate")))

    def reconnect_account(
        self,
        *,
        role_arn: str | None = None,
    ) -> AwsConnectionAuthorizationResponse:
        request = AwsConnectionReconnectRequest(role_arn=role_arn)
        return AwsConnectionAuthorizationResponse.model_validate(
            self.channel.post(
                self._aws_path("/reconnect"),
                request.model_dump(mode="json"),
            )
        )

    def cancel_reconnect(self) -> AwsConnectionResponse:
        return AwsConnectionResponse.model_validate(
            self.channel.request("DELETE", self._aws_path("/reconnect"))
        )

    def remove_account(self) -> AwsConnectionResponse | None:
        response = AwsConnectionCurrentResponse.model_validate(
            self.channel.request("DELETE", self._aws_path(""))
        )
        return response.connection

    def retry_connection(self) -> AwsConnectionResponse:
        return AwsConnectionResponse.model_validate(self.channel.post(self._aws_path("/retry")))

    def policy(self) -> WorkspaceComputePolicyResponse:
        return WorkspaceComputePolicyResponse.model_validate(
            self.channel.get(self._compute_path("/policy"))
        )

    def update_policy(
        self,
        request: WorkspaceComputePolicyUpdateRequest,
    ) -> WorkspaceComputePolicyResponse:
        return WorkspaceComputePolicyResponse.model_validate(
            self.channel.request(
                "PUT",
                self._compute_path("/policy"),
                payload=request.model_dump(mode="json"),
            )
        )

    def patch_policy(
        self,
        request: WorkspaceComputePolicyPatchRequest,
    ) -> WorkspaceComputePolicyResponse:
        return WorkspaceComputePolicyResponse.model_validate(
            self.channel.request(
                "PATCH",
                self._compute_path("/policy"),
                payload=request.model_dump(mode="json", exclude_none=True),
            )
        )

    def catalog(self) -> ComputeCatalogResponse:
        return ComputeCatalogResponse.model_validate(
            self.channel.get(self._compute_path("/catalog"))
        )

    def summary(self) -> WorkspaceComputeSummaryResponse:
        return WorkspaceComputeSummaryResponse.model_validate(
            self.channel.get(self._compute_path("/summary"))
        )

    def instances(self) -> WorkspaceComputeInstanceListResponse:
        return WorkspaceComputeInstanceListResponse.model_validate(
            self.channel.get(self._compute_path("/instances"))
        )

    def workloads(self) -> WorkspaceComputeWorkloadListResponse:
        return WorkspaceComputeWorkloadListResponse.model_validate(
            self.channel.get(self._compute_path("/workloads"))
        )

    def remove_machine(self, machine_id: str) -> None:
        self.channel.request("DELETE", self._machines_path(f"/{machine_id}"))

    def machine_join_command(
        self,
        request: MachineJoinCommandRequest,
    ) -> MachineJoinCommandResponse:
        return MachineJoinCommandResponse.model_validate(
            self.channel.post(
                self._machines_path("/join-command"),
                request.model_dump(mode="json"),
            )
        )

    def list_pool_offers(
        self,
        pool_name: str,
        request: PoolOfferQuery,
    ) -> PoolOfferListResponse:
        query = urlencode(request.model_dump(mode="json"), doseq=True)
        return PoolOfferListResponse.model_validate(
            self.channel.get(f"{self._pools_path(f'/{pool_name}/offers')}&{query}")
        )

    def launch_pool_capacity(
        self,
        pool_name: str,
        request: PoolCapacityLaunchRequest,
    ) -> PoolCapacityResponse:
        return PoolCapacityResponse.model_validate(
            self.channel.post(
                self._pools_path(f"/{pool_name}/capacity"),
                request.model_dump(mode="json"),
            )
        )

    def extend_pool_capacity(
        self,
        pool_name: str,
        request: PoolCapacityExtendRequest,
    ) -> PoolCapacityResponse:
        return PoolCapacityResponse.model_validate(
            self.channel.request(
                "PATCH",
                self._pools_path(f"/{pool_name}/capacity"),
                payload=request.model_dump(mode="json"),
            )
        )

    def scale_pool(
        self,
        pool_name: str,
        request: PoolScaleRequest,
    ) -> PoolScaleResponse:
        return PoolScaleResponse.model_validate(
            self.channel.request(
                "PUT",
                self._pools_path(f"/{pool_name}/scale"),
                payload=request.model_dump(mode="json"),
            )
        )

    def get_pool_state(self, pool_name: str) -> PoolScaleResponse:
        return PoolScaleResponse.model_validate(
            self.channel.get(self._pools_path(f"/{pool_name}/state"))
        )

    def create_pool_join_token(
        self,
        pool_name: str,
        request: PoolJoinTokenRequest,
    ) -> PoolJoinTokenResponse:
        return PoolJoinTokenResponse.model_validate(
            self.channel.post(
                self._pools_path(f"/{pool_name}/join-token"),
                request.model_dump(mode="json"),
            )
        )

    def revoke_pool_join_token(self, pool_name: str) -> None:
        self.channel.request("DELETE", self._pools_path(f"/{pool_name}/join-token"))

    def pool_join_command(
        self,
        pool_name: str,
        request: PoolJoinCommandRequest,
    ) -> PoolJoinCommandResponse:
        return PoolJoinCommandResponse.model_validate(
            self.channel.post(
                self._pools_path(f"/{pool_name}/join-command"),
                request.model_dump(mode="json"),
            )
        )

    def list_pool_machines(
        self,
        pool_name: str,
        *,
        limit: int = 100,
        cursor: str = "",
    ) -> PoolMachineListResponse:
        query = urlencode({"limit": limit, "cursor": cursor})
        return PoolMachineListResponse.model_validate(
            self.channel.get(f"{self._pools_path(f'/{pool_name}/machines')}&{query}")
        )

    def cordon_worker(self, worker_id: str) -> WorkerResponse:
        return WorkerResponse.model_validate(
            self.channel.post(self._workers_path(f"/{worker_id}/cordon"))
        )

    def uncordon_worker(self, worker_id: str) -> WorkerResponse:
        return WorkerResponse.model_validate(
            self.channel.post(self._workers_path(f"/{worker_id}/uncordon"))
        )

    def drain_worker(self, worker_id: str) -> WorkerDrainResponse:
        return WorkerDrainResponse.model_validate(
            self.channel.post(self._workers_path(f"/{worker_id}/drain"))
        )

    def _aws_path(self, suffix: str) -> str:
        return self._path(f"/api/v1/aws-connection{suffix}")

    def _machines_path(self, suffix: str) -> str:
        return self._path(f"/api/v1/machines{suffix}")

    def _pools_path(self, suffix: str) -> str:
        return self._path(f"/api/v1/pools{suffix}")

    def _workers_path(self, suffix: str) -> str:
        return self._path(f"/api/v1/workers{suffix}")

    def _compute_path(self, suffix: str) -> str:
        return self._path(f"/api/v1/compute{suffix}")

    def _path(self, path: str) -> str:
        return f"{path}?{urlencode({'workspace': self.workspace})}"


__all__ = ["ComputeClient", "ComputeControlChannel"]

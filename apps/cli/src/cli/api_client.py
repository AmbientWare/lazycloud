from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from lazycloud.control import resolve_control_client_config
from shared.autoscaler_state import AutoscalerTargetKind
from shared.http.collections import MapCollectionListResponse, SimpleQueueListResponse
from shared.http.compute import (
    ContainerDetailResponse,
    ContainerResponse,
    ContainerRunRequest,
    ContainerWithAppPageResponse,
    MachineCreateRequest,
    MachineJoinCommandRequest,
    MachineJoinTokenResponse,
    MachineListResponse,
    MachineResponse,
    UnitCreateRequest,
    UnitJoinCommandRequest,
    UnitJoinCommandResponse,
    UnitJoinTokenRequest,
    UnitJoinTokenResponse,
    UnitListResponse,
    UnitResponse,
    UnitScaleResponse,
    WorkerListResponse,
)
from shared.http.concurrency import (
    ConcurrencyAcquireResponse,
    ConcurrencyLimitListResponse,
    ConcurrencyLimitResponse,
    ConcurrencyLimitSetRequest,
)
from shared.http.deployments import DeploymentListResponse, DeploymentResponse
from shared.http.observability import (
    EventHistoryRequest,
    EventQueryResponse,
    LogQueryRequest,
    LogQueryResponse,
)
from shared.http.operations import (
    AgentLeaseListResponse,
    AgentLeaseRequest,
    AgentLeaseResponse,
    AgentListResponse,
    AgentRegisterRequest,
    AgentResponse,
    AutoscalerControlResponse,
    AutoscalerHistoryResponse,
    AutoscalerReconcileResponse,
    AutoscalerStatusListResponse,
    CronJobListResponse,
    CronJobRunListResponse,
    ImageBuildListResponse,
    ImageBuildRequest,
    ImageBuildResponse,
    SchedulerContainerDispatchListResponse,
)
from shared.http.source_cache_cleanup import SourceCacheCleanupStatusResponse
from shared.http.storage import (
    CacheContentResponse,
    CacheCreateRequest,
    CacheEntryListResponse,
    CacheEntryResponse,
    ObjectContentResponse,
    ObjectCreateRequest,
    ObjectListResponse,
    ObjectResponse,
)
from shared.http.stubs import StubCreateRequest, StubListResponse, StubResponse
from shared.http.system import (
    AuthTokenResponse,
    TokenCreateRequest,
    TokenCreateResponse,
    TokenListResponse,
)
from shared.http.usage import UsageRecordListResponse, UsageSummaryResponse
from shared.http.users import (
    SessionCreateRequest,
    SessionResponse,
    UserCreateRequest,
    UserResponse,
)
from shared.http.workspaces import (
    WorkspaceConfigExportResponse,
    WorkspaceCreateRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceSetRequest,
)
from shared.http_transport import HttpChannel
from shared.urls import url_path_segment
from shared.usage import UsageMetric

type QueryScalar = str | int | float | bool | None


@dataclass(slots=True)
class AdminApiClient:
    channel: HttpChannel
    workspace: str

    @classmethod
    def from_profile(cls, *, workspace: str | None = None) -> AdminApiClient:
        # Passed through rather than resolved against the profile first: the profile
        # always answers, so resolving here filled the highest-precedence slot and the
        # workspace environment below it was never consulted. An administrator running
        # in a container that names its workspace silently addressed `default`.
        config = resolve_control_client_config(workspace=workspace)
        return cls(
            channel=HttpChannel(
                endpoint=config.endpoint,
                token=config.token,
                timeout_seconds=config.timeout_seconds,
            ),
            workspace=config.workspace,
        )

    def list_queues(self) -> SimpleQueueListResponse:
        return SimpleQueueListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/simplequeues"))
        )

    def list_maps(self) -> MapCollectionListResponse:
        return MapCollectionListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/maps"))
        )

    def create_workspace(self, request: WorkspaceCreateRequest) -> WorkspaceResponse:
        return WorkspaceResponse.model_validate(
            self.channel.post("/api/v1/workspaces", request.model_dump(mode="json"))
        )

    def upsert_workspace(self, name: str, request: WorkspaceSetRequest) -> WorkspaceResponse:
        return WorkspaceResponse.model_validate(
            self.channel.request(
                "PUT",
                f"/api/v1/workspaces/{url_path_segment(name)}",
                payload=request.model_dump(mode="json"),
            )
        )

    def list_workspaces(self, *, include_deleted: bool = False) -> WorkspaceListResponse:
        return WorkspaceListResponse.model_validate(
            self.channel.get(
                f"/api/v1/workspaces?{urlencode({'include_deleted': include_deleted})}"
            )
        )

    def get_workspace(self, workspace_id_or_name: str) -> WorkspaceResponse:
        return WorkspaceResponse.model_validate(
            self.channel.get(f"/api/v1/workspaces/{url_path_segment(workspace_id_or_name)}")
        )

    def get_source_cache_cleanup_status(
        self,
        workspace_id_or_name: str,
    ) -> SourceCacheCleanupStatusResponse:
        return SourceCacheCleanupStatusResponse.model_validate(
            self.channel.get(
                f"/api/v1/workspaces/{url_path_segment(workspace_id_or_name)}/source-cache-cleanup"
            )
        )

    def export_workspace(self) -> WorkspaceConfigExportResponse:
        return WorkspaceConfigExportResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/workspaces/export"))
        )

    def create_stub(self, request: StubCreateRequest) -> StubResponse:
        return StubResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/stubs"),
                request.model_dump(mode="json"),
            )
        )

    def list_stubs(self) -> StubListResponse:
        return StubListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/stubs"))
        )

    def set_concurrency_limit(
        self,
        request: ConcurrencyLimitSetRequest,
    ) -> ConcurrencyLimitResponse:
        return ConcurrencyLimitResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/concurrency-limits"),
                request.model_dump(mode="json"),
            )
        )

    def list_concurrency_limits(self) -> ConcurrencyLimitListResponse:
        return ConcurrencyLimitListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/concurrency-limits"))
        )

    def acquire_concurrency(self, limit_id_or_name: str) -> ConcurrencyAcquireResponse:
        return ConcurrencyAcquireResponse.model_validate(
            self.channel.post(
                self._workspace_path(
                    f"/api/v1/concurrency-limits/{url_path_segment(limit_id_or_name)}/acquire"
                )
            )
        )

    def release_concurrency(self, limit_id_or_name: str) -> ConcurrencyAcquireResponse:
        return ConcurrencyAcquireResponse.model_validate(
            self.channel.post(
                self._workspace_path(
                    f"/api/v1/concurrency-limits/{url_path_segment(limit_id_or_name)}/release"
                )
            )
        )

    def create_user(self, request: UserCreateRequest) -> UserResponse:
        return UserResponse.model_validate(
            self.channel.post("/api/v1/users", request.model_dump(mode="json"))
        )

    def sign_in(self, request: SessionCreateRequest) -> SessionResponse:
        return SessionResponse.model_validate(
            self.channel.post("/api/v1/sessions", request.model_dump(mode="json"))
        )

    def list_tokens(self) -> TokenListResponse:
        return TokenListResponse.model_validate(self.channel.get("/api/v1/tokens/all"))

    def create_token(self, request: TokenCreateRequest) -> TokenCreateResponse:
        return TokenCreateResponse.model_validate(
            self.channel.post("/api/v1/tokens", request.model_dump(mode="json"))
        )

    def revoke_token(self, token_id: str) -> AuthTokenResponse:
        return AuthTokenResponse.model_validate(
            self.channel.post(f"/api/v1/tokens/{url_path_segment(token_id)}/revoke")
        )

    def run_container(self, request: ContainerRunRequest) -> ContainerResponse:
        return ContainerResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/containers"),
                request.model_dump(mode="json"),
            )
        )

    def list_containers(
        self,
        *,
        limit: int,
        cursor: str | None = None,
    ) -> ContainerWithAppPageResponse:
        return ContainerWithAppPageResponse.model_validate(
            self.channel.get(
                self._workspace_path(
                    "/api/v1/containers",
                    limit=limit,
                    cursor=cursor,
                )
            )
        )

    def get_container(self, container_id: str) -> ContainerDetailResponse:
        return ContainerDetailResponse.model_validate(
            self.channel.get(
                self._workspace_path(f"/api/v1/containers/{url_path_segment(container_id)}")
            )
        )

    def stop_container(self, container_id: str) -> ContainerResponse:
        return ContainerResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/containers/{url_path_segment(container_id)}/stop")
            )
        )

    def delete_container(self, container_id: str) -> None:
        self.channel.delete(
            self._workspace_path(f"/api/v1/containers/{url_path_segment(container_id)}")
        )

    def list_units(self) -> UnitListResponse:
        return UnitListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/units"))
        )

    def create_unit(self, request: UnitCreateRequest) -> UnitResponse:
        return UnitResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/units"),
                request.model_dump(mode="json"),
            )
        )

    def delete_unit(self, unit_id: str) -> None:
        self.channel.delete(self._workspace_path(f"/api/v1/units/{url_path_segment(unit_id)}"))

    def clear_unit_degradation(self, unit_id: str) -> UnitScaleResponse:
        return UnitScaleResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/units/{url_path_segment(unit_id)}/clear-degradation")
            )
        )

    def create_pool_join_token(
        self,
        request: MachineJoinCommandRequest,
    ) -> MachineJoinTokenResponse:
        return MachineJoinTokenResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/machines/join-token"),
                request.model_dump(mode="json"),
            )
        )

    def create_unit_join_token(
        self,
        unit_id: str,
        request: UnitJoinTokenRequest,
    ) -> UnitJoinTokenResponse:
        return UnitJoinTokenResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/units/{url_path_segment(unit_id)}/join-token"),
                request.model_dump(mode="json"),
            )
        )

    def unit_join_command(
        self,
        unit_id: str,
        request: UnitJoinCommandRequest,
    ) -> UnitJoinCommandResponse:
        return UnitJoinCommandResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/units/{url_path_segment(unit_id)}/join-command"),
                request.model_dump(mode="json"),
            )
        )

    def list_machines(self) -> MachineListResponse:
        return MachineListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/machines"))
        )

    def create_machine(self, request: MachineCreateRequest) -> MachineResponse:
        return MachineResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/machines"),
                request.model_dump(mode="json"),
            )
        )

    def delete_machine(self, machine_id: str) -> None:
        self.channel.delete(
            self._workspace_path(f"/api/v1/machines/{url_path_segment(machine_id)}")
        )

    def list_workers(self) -> WorkerListResponse:
        return WorkerListResponse.model_validate(self.channel.get("/api/v1/workers"))

    def delete_worker(self, worker_id: str) -> None:
        self.channel.delete(f"/api/v1/workers/{url_path_segment(worker_id)}")

    def list_deployments(
        self,
        *,
        app_id: str | None = None,
        limit: int = 100,
    ) -> DeploymentListResponse:
        return DeploymentListResponse.model_validate(
            self.channel.get(
                self._workspace_path("/api/v1/deployments", app_id=app_id, limit=limit)
            )
        )

    def get_deployment(self, deployment_id: str) -> DeploymentResponse:
        return DeploymentResponse.model_validate(
            self.channel.get(
                self._workspace_path(f"/api/v1/deployments/{url_path_segment(deployment_id)}")
            )
        )

    def logs(self, request: LogQueryRequest) -> LogQueryResponse:
        return LogQueryResponse.model_validate(
            self.channel.get(
                _query_path(
                    "/api/v1/logs",
                    {
                        "workspace_id": request.workspace_id,
                        "object_id": request.object_id,
                        "object_type": (
                            request.object_type.value if request.object_type is not None else None
                        ),
                        "stub_id": request.stub_id,
                        "app_id": request.app_id,
                        "task_id": request.task_id,
                        "container_id": request.container_id,
                        "machine_id": request.machine_id,
                        "worker_id": request.worker_id,
                        "query": request.query,
                        "limit": request.limit,
                        "page": request.page,
                        "start_time": (
                            request.start_time.isoformat()
                            if request.start_time is not None
                            else None
                        ),
                        "end_time": (
                            request.end_time.isoformat() if request.end_time is not None else None
                        ),
                        "cursor": request.cursor,
                        "seq_num": request.seq_num,
                        "wait": request.wait,
                        "clamp": request.clamp,
                    },
                )
            )
        )

    def events(self, request: EventHistoryRequest) -> EventQueryResponse:
        return EventQueryResponse.model_validate(
            self.channel.get(
                _query_path(
                    "/api/v1/events/history",
                    {
                        "workspace_id": request.workspace_id,
                        "resource_type": request.resource_type,
                        "resource_id": request.resource_id,
                        "task_id": request.task_id,
                        "container_id": request.container_id,
                        "limit": request.limit,
                        "cursor": request.cursor,
                    },
                )
            )
        )

    def create_image_build(self, request: ImageBuildRequest) -> ImageBuildResponse:
        return ImageBuildResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/image-builds"),
                request.model_dump(mode="json"),
            )
        )

    def list_image_builds(self) -> ImageBuildListResponse:
        return ImageBuildListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/image-builds"))
        )

    def list_cron_jobs(self) -> CronJobListResponse:
        return CronJobListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/cron-jobs"))
        )

    def delete_cron_job(self, name: str) -> None:
        self.channel.delete(self._workspace_path(f"/api/v1/cron-jobs/{url_path_segment(name)}"))

    def list_cron_job_runs(self) -> CronJobRunListResponse:
        return CronJobRunListResponse.model_validate(self.channel.get("/api/v1/cron-job-runs"))

    def tick_scheduler(self) -> CronJobRunListResponse:
        return CronJobRunListResponse.model_validate(self.channel.post("/api/v1/scheduler/tick"))

    def dispatch_scheduler_containers(
        self,
        *,
        limit: int,
    ) -> SchedulerContainerDispatchListResponse:
        return SchedulerContainerDispatchListResponse.model_validate(
            self.channel.post(
                f"/api/v1/scheduler/containers/dispatch?{urlencode({'limit': limit})}"
            )
        )

    def autoscaler_status(
        self,
        *,
        target_kind: AutoscalerTargetKind | None,
        target_id: str | None,
    ) -> AutoscalerStatusListResponse:
        return AutoscalerStatusListResponse.model_validate(
            self.channel.get(
                self._workspace_path(
                    "/api/v1/scheduler/autoscalers",
                    target_kind=target_kind.value if target_kind is not None else None,
                    target_id=target_id,
                )
            )
        )

    def autoscaler_history(
        self,
        *,
        target_id: str | None,
        limit: int,
    ) -> AutoscalerHistoryResponse:
        return AutoscalerHistoryResponse.model_validate(
            self.channel.get(
                self._workspace_path(
                    "/api/v1/scheduler/autoscalers/history",
                    target_id=target_id,
                    limit=limit,
                )
            )
        )

    def reconcile_autoscalers(
        self,
        *,
        target_kind: AutoscalerTargetKind | None,
        stub_id: str | None,
    ) -> AutoscalerReconcileResponse:
        return AutoscalerReconcileResponse.model_validate(
            self.channel.post(
                self._workspace_path(
                    "/api/v1/scheduler/autoscalers/reconcile",
                    target_kind=target_kind.value if target_kind is not None else None,
                    stub_id=stub_id,
                )
            )
        )

    def control_autoscaler(
        self,
        stub_id_or_name: str,
        *,
        action: str,
    ) -> AutoscalerControlResponse:
        if action not in {"pause", "resume"}:
            raise ValueError(f"invalid autoscaler action: {action}")
        return AutoscalerControlResponse.model_validate(
            self.channel.post(
                self._workspace_path(
                    f"/api/v1/scheduler/autoscalers/{url_path_segment(stub_id_or_name)}/{action}"
                )
            )
        )

    def list_agents(self) -> AgentListResponse:
        return AgentListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/agents"))
        )

    def register_agent(self, request: AgentRegisterRequest) -> AgentResponse:
        return AgentResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/agents"),
                request.model_dump(mode="json"),
            )
        )

    def heartbeat_agent(self, agent_id: str) -> AgentResponse:
        return AgentResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/agents/{url_path_segment(agent_id)}/heartbeat")
            )
        )

    def lease_agent(self, agent_id: str, request: AgentLeaseRequest) -> AgentLeaseResponse:
        return AgentLeaseResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/agents/{url_path_segment(agent_id)}/leases"),
                request.model_dump(mode="json"),
            )
        )

    def delete_agent(self, agent_id: str) -> None:
        self.channel.delete(self._workspace_path(f"/api/v1/agents/{url_path_segment(agent_id)}"))

    def list_leases(self, *, include_inactive: bool) -> AgentLeaseListResponse:
        return AgentLeaseListResponse.model_validate(
            self.channel.get(
                self._workspace_path("/api/v1/leases", include_inactive=include_inactive)
            )
        )

    def release_lease(self, lease_id: str) -> AgentLeaseResponse:
        return AgentLeaseResponse.model_validate(
            self.channel.post(
                self._workspace_path(f"/api/v1/leases/{url_path_segment(lease_id)}/release")
            )
        )

    def list_objects(self, *, bucket: str | None, prefix: str) -> ObjectListResponse:
        return ObjectListResponse.model_validate(
            self.channel.get(self._workspace_path("/api/v1/objects", bucket=bucket, prefix=prefix))
        )

    def create_object(self, request: ObjectCreateRequest) -> ObjectResponse:
        return ObjectResponse.model_validate(
            self.channel.post(
                self._workspace_path("/api/v1/objects"),
                request.model_dump(mode="json"),
            )
        )

    def read_object(self, bucket: str, key: str) -> ObjectContentResponse:
        return ObjectContentResponse.model_validate(
            self.channel.get(
                self._workspace_path(
                    f"/api/v1/objects/{url_path_segment(bucket)}/{url_path_segment(key)}"
                )
            )
        )

    def delete_object(self, bucket: str, key: str) -> None:
        self.channel.delete(
            self._workspace_path(
                f"/api/v1/objects/{url_path_segment(bucket)}/{url_path_segment(key)}"
            )
        )

    def list_cache(self) -> CacheEntryListResponse:
        return CacheEntryListResponse.model_validate(self.channel.get("/api/v1/cache"))

    def create_cache_entry(self, request: CacheCreateRequest) -> CacheEntryResponse:
        return CacheEntryResponse.model_validate(
            self.channel.post("/api/v1/cache", request.model_dump(mode="json"))
        )

    def read_cache_entry(self, namespace: str, key: str) -> CacheContentResponse:
        return CacheContentResponse.model_validate(
            self.channel.get(f"/api/v1/cache/{url_path_segment(namespace)}/{url_path_segment(key)}")
        )

    def delete_cache_entry(self, namespace: str, key: str) -> None:
        self.channel.delete(f"/api/v1/cache/{url_path_segment(namespace)}/{url_path_segment(key)}")

    def list_usage_records(
        self,
        *,
        metric: UsageMetric | None,
        resource_type: str | None,
        resource_id: str | None,
        start: str | None,
        end: str | None,
        limit: int,
        cursor: str | None,
    ) -> UsageRecordListResponse:
        return UsageRecordListResponse.model_validate(
            self.channel.get(
                self._workspace_path(
                    "/api/v1/usage/records",
                    metric=metric.value if metric is not None else None,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    start=start,
                    end=end,
                    limit=limit,
                    cursor=cursor,
                )
            )
        )

    def usage_summary(
        self,
        *,
        metric: UsageMetric | None,
        resource_type: str | None,
        resource_id: str | None,
        start: str | None,
        end: str | None,
    ) -> UsageSummaryResponse:
        return UsageSummaryResponse.model_validate(
            self.channel.get(
                self._workspace_path(
                    "/api/v1/usage/summary",
                    metric=metric.value if metric is not None else None,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    start=start,
                    end=end,
                )
            )
        )

    def _workspace_path(self, path: str, **params: QueryScalar) -> str:
        return _query_path(path, {"workspace": self.workspace, **params})


def admin_api_client(workspace: str | None = None) -> AdminApiClient:
    return AdminApiClient.from_profile(workspace=workspace)


def _query_path(path: str, params: Mapping[str, QueryScalar]) -> str:
    selected = {key: value for key, value in params.items() if value is not None}
    return f"{path}?{urlencode(selected, doseq=True)}" if selected else path


__all__ = ["AdminApiClient", "admin_api_client"]

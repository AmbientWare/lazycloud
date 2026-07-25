from __future__ import annotations

from compute.policy import WorkspaceComputePolicyService
from fastapi import APIRouter, Depends
from shared.compute_policy import WorkspaceComputePolicy
from shared.http.compute_policy import (
    ComputeCapacitySummaryResponse,
    ComputeCatalogInstanceResponse,
    ComputeCatalogRegionResponse,
    ComputeCatalogResponse,
    ComputeConnectionSummaryResponse,
    ComputeCostSummaryResponse,
    ResolvedComputePlacementResponse,
    WorkspaceComputeInstanceListResponse,
    WorkspaceComputeInstanceResponse,
    WorkspaceComputePolicyResponse,
    WorkspaceComputePolicyUpdateRequest,
    WorkspaceComputeSummaryResponse,
    WorkspaceComputeWorkloadListResponse,
    WorkspaceComputeWorkloadResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.service_dependencies import workspace_compute_policy_service

router = APIRouter(prefix="/api/v1/compute", tags=["compute"])


def _policy_response(service_policy: WorkspaceComputePolicy) -> WorkspaceComputePolicyResponse:
    return WorkspaceComputePolicyResponse.model_validate(service_policy)


@router.get(
    "/policy",
    response_model=WorkspaceComputePolicyResponse,
    operation_id="get_workspace_compute_policy",
)
def get_workspace_compute_policy(
    workspace_id: read_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputePolicyResponse:
    return _policy_response(service.get_policy(workspace=workspace_id))


@router.put(
    "/policy",
    response_model=WorkspaceComputePolicyResponse,
    operation_id="update_workspace_compute_policy",
)
def update_workspace_compute_policy(
    request: WorkspaceComputePolicyUpdateRequest,
    workspace_id: write_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputePolicyResponse:
    return _policy_response(
        service.update_policy(
            workspace=workspace_id,
            expected_revision=request.expected_revision,
            default_placement=request.default_placement,
            aws=request.aws,
        )
    )


@router.get(
    "/catalog",
    response_model=ComputeCatalogResponse,
    operation_id="get_workspace_compute_catalog",
)
def get_workspace_compute_catalog(
    workspace_id: read_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> ComputeCatalogResponse:
    return ComputeCatalogResponse(
        data=[
            ComputeCatalogRegionResponse(
                provider="aws",
                region=region,
                instances=[
                    ComputeCatalogInstanceResponse(
                        instance_type=item.instance_type,
                        kind=item.kind,
                        cpu_millicores=item.cpu_millicores,
                        memory_mb=item.memory_mb,
                        gpu=item.gpu,
                        gpu_count=item.gpu_count,
                    )
                    for item in instances
                ],
            )
            for region, instances in service.catalog(workspace=workspace_id)
        ],
        next="",
    )


@router.get(
    "/summary",
    response_model=WorkspaceComputeSummaryResponse,
    operation_id="get_workspace_compute_summary",
)
def get_workspace_compute_summary(
    workspace_id: read_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputeSummaryResponse:
    summary = service.summary(workspace=workspace_id)
    return WorkspaceComputeSummaryResponse(
        policy=_policy_response(summary.policy),
        connection=(
            ComputeConnectionSummaryResponse(
                account_id=summary.connection.account_id,
                phase=summary.connection.phase,
            )
            if summary.connection is not None
            else None
        ),
        instances=ComputeCapacitySummaryResponse(
            total=len(summary.instances),
            ready=summary.ready_instance_count,
            pending=summary.pending_instance_count,
            degraded=summary.degraded_instance_count,
        ),
        cost=ComputeCostSummaryResponse(
            hourly_micros=summary.hourly_cost_micros,
            daily_micros=summary.hourly_cost_micros * 24,
        ),
        workload_count=summary.workload_count,
    )


@router.get(
    "/instances",
    response_model=WorkspaceComputeInstanceListResponse,
    operation_id="list_workspace_compute_instances",
)
def list_workspace_compute_instances(
    workspace_id: read_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputeInstanceListResponse:
    return WorkspaceComputeInstanceListResponse(
        data=[
            WorkspaceComputeInstanceResponse(
                id=item.record.instance_id or item.record.id,
                machine_id=item.record.machine_id,
                provider=item.record.provider,
                region=item.region,
                instance_type=item.record.instance_type,
                status=item.bootstrap_phase.value,
                gpu=item.record.gpu,
                gpu_count=item.record.gpu_count,
                cpu_millicores=item.record.cpu_millicores,
                memory_mb=item.record.memory_mb,
                bootstrap_phase=item.bootstrap_phase,
                bootstrap_failure_reason=item.bootstrap_failure_reason,
                bootstrap_observed_at=item.bootstrap_observed_at,
                launch_attempt=item.record.launch_attempt,
                created_at=item.record.created_at,
            )
            for item in service.instances(workspace=workspace_id)
        ],
        next="",
    )


@router.get(
    "/workloads",
    response_model=WorkspaceComputeWorkloadListResponse,
    operation_id="list_workspace_compute_workloads",
)
def list_workspace_compute_workloads(
    workspace_id: read_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputeWorkloadListResponse:
    return WorkspaceComputeWorkloadListResponse(
        data=[
            WorkspaceComputeWorkloadResponse(
                deployment_id=item.deployment.id,
                app_id=item.deployment.app_id,
                name=item.deployment.name,
                kind=item.deployment.kind,
                placement=ResolvedComputePlacementResponse(
                    target=item.placement.target,
                    source=item.placement.source,
                    provider=item.placement.provider,
                    region=item.placement.region,
                ),
                cpu_millicores=item.resources.cpu_millicores,
                memory_mb=item.resources.memory_mb,
                gpu=item.resources.gpu,
                gpu_count=item.resources.gpu_count,
            )
            for item in service.workloads(workspace=workspace_id)
        ],
        next="",
    )


__all__ = ["router"]

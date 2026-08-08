from __future__ import annotations

from compute.policy import WorkspaceComputePolicyService
from fastapi import APIRouter, Depends
from shared.compute_policy import AwsWorkspaceComputePolicy, WorkspaceComputePolicy
from shared.http.compute_policy import (
    ComputeCapacitySummaryResponse,
    ComputeCatalogInstanceResponse,
    ComputeCatalogRegionResponse,
    ComputeCatalogResponse,
    ComputeConnectionSummaryResponse,
    ComputeCostSummaryResponse,
    MachinePoolListResponse,
    MachinePoolResponse,
    WorkspaceComputeInstanceListResponse,
    WorkspaceComputeInstanceResponse,
    WorkspaceComputePolicyPatchRequest,
    WorkspaceComputePolicyResponse,
    WorkspaceComputePolicyUpdateRequest,
    WorkspaceComputeSummaryResponse,
    WorkspaceComputeWorkloadListResponse,
    WorkspaceComputeWorkloadResponse,
)

from api.server.auth import read_user, read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.service_dependencies import workspace_compute_policy_service
from api.server.services import ApiServices

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
            default_pool=request.default_pool,
            aws=request.aws,
        )
    )


@router.patch(
    "/policy",
    response_model=WorkspaceComputePolicyResponse,
    operation_id="patch_workspace_compute_policy",
)
def patch_workspace_compute_policy(
    request: WorkspaceComputePolicyPatchRequest,
    workspace_id: write_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputePolicyResponse:
    current = service.get_policy(workspace=workspace_id)
    changed = request.aws.model_dump(exclude_none=True)
    merged = current.aws.model_copy(update=changed) if changed else current.aws
    return _policy_response(
        service.update_policy(
            workspace=workspace_id,
            expected_revision=request.expected_revision,
            default_pool=request.default_pool or current.default_pool,
            aws=AwsWorkspaceComputePolicy.model_validate(dict(merged)),
        )
    )


@router.get(
    "/catalog",
    response_model=ComputeCatalogResponse,
    operation_id="get_workspace_compute_catalog",
)
def get_compute_catalog(
    _user_id: read_user,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> ComputeCatalogResponse:
    """What may be launched in a connected cloud: provider inventory, not tenant state."""
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
            for region, instances in service.catalog()
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
    operation_id="list_compute_instances",
)
def list_compute_instances(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> WorkspaceComputeInstanceListResponse:
    """Capacity running in this account's connected cloud, across its workspaces."""
    return WorkspaceComputeInstanceListResponse(
        data=[
            WorkspaceComputeInstanceResponse(
                id=item.record.instance_id or item.record.id,
                machine_id=item.record.machine_id,
                provider=item.record.provider,
                region=item.region,
                instance_type=item.record.instance_type,
                status=item.service_state.value,
                gpu=item.record.gpu,
                gpu_count=item.record.gpu_count,
                cpu_millicores=item.record.cpu_millicores,
                memory_mb=item.record.memory_mb,
                bootstrap_phase=item.bootstrap_phase,
                service_state=item.service_state,
                bootstrap_failure_reason=item.bootstrap_failure_reason,
                bootstrap_failure_detail=item.bootstrap_failure_detail,
                bootstrap_observed_at=item.bootstrap_observed_at,
                launch_attempt=item.record.launch_attempt,
                booted_template_version=item.booted_template_version,
                created_at=item.record.created_at,
            )
            for item in service.instances_for_account(
                workspace_ids=services.users.owned_workspace_ids(user_id)
            )
        ],
        next="",
    )


@router.get(
    "/pools",
    response_model=MachinePoolListResponse,
    operation_id="list_workspace_compute_pools",
)
def list_workspace_compute_pools(
    workspace_id: read_workspace,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> MachinePoolListResponse:
    """Pools this workspace can run workloads in."""
    return MachinePoolListResponse(
        data=[
            MachinePoolResponse(
                name=item.name,
                is_default=item.is_default,
                providers=item.providers,
                unit_count=item.unit_count,
                gpu_types=item.gpu_types,
            )
            for item in service.pools(workspace=workspace_id)
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
                pool=item.pool,
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

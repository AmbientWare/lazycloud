from __future__ import annotations

from compute.policy import WorkspaceComputePolicyService
from fastapi import APIRouter, Depends
from shared.compute_enrollment import AgentCapacityState
from shared.http.compute_policy import (
    ComputeCapacitySummaryResponse,
    ComputeCatalogInstanceResponse,
    ComputeCatalogRegionResponse,
    ComputeCatalogResponse,
    ComputeConnectionSummaryResponse,
    ComputeCostSummaryResponse,
    ConnectionMachineListResponse,
    ConnectionMachineResponse,
    WorkspaceComputeSummaryResponse,
    WorkspaceComputeWorkloadListResponse,
    WorkspaceComputeWorkloadResponse,
)
from shared.resources import parse_memory_mib

from api.server.auth import read_user, read_workspace
from api.server.service_dependencies import workspace_compute_policy_service

router = APIRouter(prefix="/api/v1/compute", tags=["compute"])


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
        connection=(
            ComputeConnectionSummaryResponse(
                account_id=summary.connection.account_id,
                phase=summary.connection.phase,
            )
            if summary.connection is not None
            else None
        ),
        instances=ComputeCapacitySummaryResponse(
            total=len(summary.machines),
            ready=summary.ready_machine_count,
            pending=summary.pending_machine_count,
            degraded=summary.degraded_machine_count,
        ),
        cost=ComputeCostSummaryResponse(
            hourly_micros=summary.hourly_cost_micros,
            daily_micros=(
                summary.hourly_cost_micros * 24 if summary.hourly_cost_micros is not None else None
            ),
        ),
        workload_count=summary.workload_count,
    )


@router.get(
    "/instances",
    response_model=ConnectionMachineListResponse,
    operation_id="list_compute_instances",
)
def list_compute_instances(
    user_id: read_user,
    service: WorkspaceComputePolicyService = Depends(workspace_compute_policy_service),
) -> ConnectionMachineListResponse:
    """Machines running in this account's connected cloud, across its workspaces."""
    return ConnectionMachineListResponse(
        data=[
            ConnectionMachineResponse(
                id=item.machine.id,
                placement=item.machine.placement,
                provider=item.machine.provider,
                region=item.instance.region if item.instance is not None else "",
                availability_zone=(
                    item.instance.availability_zone if item.instance is not None else ""
                ),
                instance_id=(item.instance.instance_id or "" if item.instance is not None else ""),
                instance_type=(
                    item.instance.instance_type or "" if item.instance is not None else ""
                ),
                lifecycle=item.machine.lifecycle,
                lifecycle_message=item.machine.lifecycle_message,
                lifecycle_failure=item.machine.lifecycle_failure,
                lifecycle_at=item.machine.lifecycle_at,
                connected=item.connected,
                capacity_state=(
                    item.enrollment.capacity_state
                    if item.enrollment is not None
                    else AgentCapacityState.Available
                ),
                capacity_reason=(
                    item.enrollment.capacity_reason if item.enrollment is not None else ""
                ),
                gpu=item.machine.gpu,
                gpu_count=item.machine.gpu_count,
                cpu_millicores=int((item.machine.cpu or 0) * 1000),
                memory_mb=parse_memory_mib(item.machine.memory) or 0,
                launch_attempt=item.instance.launch_attempt if item.instance is not None else 1,
                booted_template_version=(
                    item.instance.booted_template_version if item.instance is not None else ""
                ),
                launched_at=item.instance.created_at if item.instance is not None else None,
                created_at=item.machine.created_at,
            )
            for item in service.connection_machines(user_id=user_id)
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
                machine=item.machine,
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

from __future__ import annotations

from typing import Annotated

from compute.aws_connections import AwsAccountConnectionDirectory, AwsAccountConnectionService
from compute.policy import WorkspaceComputePolicyService
from control.service import ControlPlaneService
from execution.artifacts.service import ArtifactStorageService
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from execution.pods.service import PodControlService
from execution.shells.service import ShellControlService
from execution.signals.redis import RedisSignalService
from execution.task_rerun import TaskRerunService
from execution.volumes.control import VolumeControlService
from fastapi import Depends
from gateway.machine_lifecycle import MachineLifecycleService
from gateway.provider_enrollment import ProviderNodeEnrollmentService
from gateway.service import GatewayControlService
from images.control import ImageControlService
from networking.dialer import BackendRouteDialerConfig
from scheduler.autoscaler_operations import AutoscalerOperationsService
from scheduler.routes import SchedulerBackendRouteResolver
from shared.errors import UpstreamUnavailableError

from api.server.dependencies import api_services
from api.server.services import (
    ApiServices,
    ApiTailnetRuntime,
    EndpointApiService,
    FunctionApiService,
    TaskQueueApiService,
)
from api.server.worker_repository_service import (
    WorkerRepositoryService,
)


def endpoint_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> EndpointApiService:
    return services.endpoint_service


def control_plane_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> ControlPlaneService:
    return services.control_plane_service


def aws_account_connection_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> AwsAccountConnectionService:
    if services.aws_connections is None:
        raise UpstreamUnavailableError("AWS account connections are not enabled")
    return services.aws_connections


def aws_account_connection_directory(
    services: Annotated[ApiServices, Depends(api_services)],
) -> AwsAccountConnectionDirectory:
    return services.aws_account_connection_directory


def workspace_compute_policy_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> WorkspaceComputePolicyService:
    return services.workspace_compute_policy_service


def provider_node_enrollment_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> ProviderNodeEnrollmentService:
    if services.provider_node_enrollment_service is None:
        raise UpstreamUnavailableError("AWS workspace compute is not enabled")
    return services.provider_node_enrollment_service


def machine_lifecycle_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> MachineLifecycleService:
    return services.machine_lifecycle_service


def function_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> FunctionApiService:
    return services.function_service


def gateway_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> GatewayControlService:
    return services.gateway_service


def image_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> ImageControlService:
    return services.image_service


def artifact_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> ArtifactStorageService:
    return services.artifact_service


def pod_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> PodControlService:
    return services.pod_service


def signal_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> RedisSignalService:
    return services.signal_service


def map_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> RedisMapService:
    return services.map_service


def simple_queue_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> RedisSimpleQueueService:
    return services.simple_queue_service


def shell_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> ShellControlService:
    return services.shell_service


def backend_route_resolver(
    services: Annotated[ApiServices, Depends(api_services)],
) -> SchedulerBackendRouteResolver:
    return services.backend_route_resolver


def backend_route_dialer_config(
    services: Annotated[ApiServices, Depends(api_services)],
) -> BackendRouteDialerConfig:
    return services.backend_route_dialer_config


def tailnet_runtime_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> ApiTailnetRuntime | None:
    return services.tailnet_runtime


def taskqueue_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> TaskQueueApiService:
    return services.taskqueue_service


def task_rerun_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> TaskRerunService:
    return services.task_rerun_service


def volume_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> VolumeControlService:
    return services.volume_service


def worker_repository_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> WorkerRepositoryService:
    return services.worker_repository_service


def autoscaler_operations_service(
    services: Annotated[ApiServices, Depends(api_services)],
) -> AutoscalerOperationsService:
    return services.autoscaler_operations_service

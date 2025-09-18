import yaml
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from lazycloud_api.api.security import (
    UserData,
    check_user_id_request,
    get_current_active_user,
)
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.prefect_app.compose import (
    deploy_compose_task,
    destroy_compose_task,
)
from lazycloud_api.services.compose.diff_checker import ComposeDiffChecker
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name
from lazycloud_api.services.k8s.workload_operations import WorkloadOperations
from shared.models.deployments import DeploymentStates
from shared.models.diffs import EnvVarChanges
from shared.models.statuses import DeploymentStatus, KubernetesPhase, TaskStatus
from shared.models.tasks import DeploymentTaskStatusResponse
from shared.responses.deployments import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
    DiffResponse,
    RestartResponse,
    RestartServiceResult,
)

deployments_router = APIRouter(prefix="/deployments", tags=["deployments"])


# Pydantic Models
class DeploymentCreateRequest(BaseModel):
    compose_yaml: str = Field(..., description="Docker Compose YAML content")
    name: str | None = Field(
        None,
        description="Unique deployment name for updates",
        pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$",
        max_length=63,
    )
    secrets: bool = Field(False, description="Whether to wait for secrets to be stored")


class DiffRequest(BaseModel):
    compose_yaml: str = Field(..., description="New Docker Compose YAML content")
    deployment_name: str | None = Field(
        None, description="Deployment name (required when deployment_id is 'new')"
    )
    env_keys: list[str] = Field(
        default_factory=list, description="List of environment variable keys"
    )


@deployments_router.post("/deployments", response_model=DeploymentTaskStatusResponse)
async def create_deployment(
    request: DeploymentCreateRequest,
    current_user: UserData = Depends(get_current_active_user),
) -> DeploymentTaskStatusResponse:
    """Create a new compose deployment."""
    try:
        # Parse compose file
        compose_data = yaml.safe_load(request.compose_yaml)

        # Generate namespace as lazycloud-[user_id]
        # This gives each user their own namespace for all deployments
        namespace = create_ns_name(current_user.user_id)

        # Validate compose file with user context
        compose_file = ComposeParser.parse_dict(compose_data)

        # Validate the compose file
        validator = ComposeValidator()
        validation_result = validator.validate_to_result(compose_file)

        # If there are errors, return them
        if not validation_result.can_deploy:
            raise ValueError(
                f"Validation errors: {'; '.join(validation_result.errors)}"
            )

        # Check if this is an update to an existing deployment
        deployment = None
        if request.name:
            filters = {
                "user_id": current_user.user_id,
                "name": request.name,
            }
            # Try to find existing deployment by name
            deployment = await db.compose_deployments.afind_one(filters=filters)

        if deployment:
            # Update existing deployment
            deployment.compose_yaml = request.compose_yaml
            deployment.state = DeploymentStates.PENDING
            deployment.status_message = "Update queued"
            deployment = await db.compose_deployments.aupdate(deployment)

        else:
            # Create new deployment
            deployment_data = ComposeDeploymentPydantic(
                user_id=current_user.user_id,
                name=request.name,
                namespace=namespace,
                compose_yaml=request.compose_yaml,
                state=DeploymentStates.PENDING,
                status_message="Deployment queued",
            )
            deployment = await db.compose_deployments.acreate(deployment_data)

        if not deployment:
            raise HTTPException(status_code=500, detail="Deployment not found")

        # Queue deployment task
        task_future = deploy_compose_task.delay(
            deployment_id=str(deployment.id),
            wait_for_secrets=request.secrets,
        )

        return DeploymentTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message="Deployment task queued",
            deployment_id=str(deployment.id),
        )

    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {str(e)}")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    except Exception as e:
        logger.error(f"Failed to create deployment: {e}")
        raise HTTPException(status_code=500, detail="Failed to create deployment")


@deployments_router.get("/deployments", response_model=DeploymentListResponse)
async def list_deployments(
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    user_id: str | None = None,
    deployment_id: str | None = None,
    name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> DeploymentListResponse:
    """List compose deployments."""
    user_id = await check_user_id_request(user_id, current_user)

    # append all search params if they are not None, always include user_id
    search_params = {"user_id": user_id}
    if status:
        search_params["status"] = status
    if deployment_id:
        search_params["id"] = deployment_id
    if name:
        search_params["name"] = name

    try:
        deployments = await db.compose_deployments.afind(search_params)

        # Apply pagination
        total = len(deployments)
        paginated_deployments = deployments[skip : skip + limit]

        deployment_responses = [
            DeploymentResponse(
                id=deployment.id,
                user_id=deployment.user_id,
                name=deployment.name,
                namespace=deployment.namespace,
                state=deployment.state,
                status_message=deployment.status_message,
                deployed_at=deployment.deployed_at,
                created_at=deployment.created_at,
                updated_at=deployment.updated_at,
            )
            for deployment in paginated_deployments
            if deployment.id is not None and deployment.name is not None
        ]

        return DeploymentListResponse(
            deployments=deployment_responses,
            total=total,
            skip=skip,
            limit=limit,
        )

    except Exception as e:
        logger.error(f"Failed to list deployments: {e}")
        raise HTTPException(status_code=500, detail="Failed to list deployments")


@deployments_router.delete(
    "/deployments/{deployment_id}", response_model=DeploymentTaskStatusResponse
)
async def delete_deployment(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
) -> DeploymentTaskStatusResponse:
    """Delete a deployment."""
    try:
        deployment = await db.compose_deployments.aget_by_id(deployment_id)

        if not deployment or deployment.user_id != current_user.user_id:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Update status to deleting
        await db.compose_deployments.update_status(
            deployment_id, DeploymentStates.DEPLOYING, "Deletion initiated"
        )

        # Queue deletion task
        task_future = destroy_compose_task.delay(
            deployment_id=deployment_id,
            user_id=current_user.user_id,
        )

        return DeploymentTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message="Deletion task queued",
            deployment_id=deployment_id,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to delete deployment: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete deployment")


@deployments_router.post(
    "/deployments/{deployment_id}/diff", response_model=DiffResponse
)
async def diff_deployment(
    deployment_id: str,
    request: DiffRequest,
    current_user: UserData = Depends(get_current_active_user),
) -> DiffResponse:
    """Compare current deployment with proposed changes."""
    # Special case: deployment_id == "new" means this is a new deployment
    if deployment_id == "new":
        # For new deployments, we need the deployment name
        if not request.deployment_name:
            raise HTTPException(
                status_code=400,
                detail="deployment_name is required for new deployments",
            )

        deployment = None

    else:
        deployment = await db.compose_deployments.aget_by_id(deployment_id)
        if not deployment or deployment.user_id != current_user.user_id:
            raise HTTPException(status_code=404, detail="Deployment not found")

    # Get user's UUID from usage table
    user_usage = await db.usage.afind_one(filters={"user_id": current_user.user_id})
    if not user_usage or not user_usage.uuid:
        raise HTTPException(status_code=400, detail="User usage record not found")

    # Parse new compose file
    compose_data = yaml.safe_load(request.compose_yaml)
    compose_file = ComposeParser.parse_dict(compose_data)

    # Validate the compose file
    validator = ComposeValidator()
    validation_result = validator.validate_to_result(compose_file)

    # Determine namespace
    namespace = create_ns_name(current_user.user_id)

    # Handle environment variable diff
    env_var_changes = None
    if deployment and deployment_id != "new":
        # Get existing secrets from database
        existing_secret = await db.secrets.aget_secret(deployment_id)
        existing_keys = (
            set(existing_secret.secrets.keys()) if existing_secret else set()
        )
        new_keys = set(request.env_keys)

        env_var_changes = EnvVarChanges(
            added=sorted(list(new_keys - existing_keys)),
            removed=sorted(list(existing_keys - new_keys)),
            existing=sorted(list(new_keys & existing_keys)),
        )
    else:
        # For new deployments, all keys are "added"
        env_var_changes = EnvVarChanges(
            added=sorted(request.env_keys), removed=[], existing=[]
        )

    # Get current compose file for comparison
    current_compose = None
    if deployment:
        try:
            current_compose_data = yaml.safe_load(deployment.compose_yaml)
            current_compose = ComposeParser.parse_dict(current_compose_data)
        except Exception as e:
            logger.warning(f"Failed to parse current compose file: {e}")
            current_compose = None

    # Perform Compose-level diff
    compose_diff_checker = ComposeDiffChecker()
    compose_diff = compose_diff_checker.compare_compose_files(
        current_compose, compose_file
    )

    return DiffResponse(
        deployment_id=deployment_id,
        namespace=namespace,
        has_changes=compose_diff.has_changes(),
        diff=compose_diff,
        env_var_changes=env_var_changes,
        errors=validation_result.errors,
        warnings=validation_result.warnings,
    )


@deployments_router.post(
    "/deployments/{deployment_id}/restart/{service_name}",
    response_model=RestartResponse,
)
async def restart_service(
    deployment_id: str,
    service_name: str,
    current_user: UserData = Depends(get_current_active_user),
) -> RestartResponse:
    """Restart a specific service within a deployment."""
    try:
        # Get deployment from database
        deployment = await db.compose_deployments.afind_one(
            {
                "id": deployment_id,
                "user_id": current_user.user_id,
            }
        )

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Check if service exists
        service_names = [service.name for service in deployment.helm_values.services]
        if service_name not in service_names:
            raise HTTPException(
                status_code=404,
                detail=f"Service '{service_name}' not found in deployment",
            )

        # Find the service by name
        service = next(
            s for s in deployment.helm_values.services if s.name == service_name
        )

        # Use WorkloadOperations to restart the service
        workload_ops = WorkloadOperations()
        result = workload_ops.restart_service(
            service=service,
            namespace=deployment.namespace,
        )

        return RestartResponse(
            deployment_id=deployment_id,
            deployment_name=deployment.name,
            service_name=service_name,
            success=result.success,
            message=result.message,
            resource_type=result.resource_type,
            resource_name=result.resource_name,
            output=result.output,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to restart service: {e}")
        raise HTTPException(status_code=500, detail="Failed to restart service")


@deployments_router.post(
    "/deployments/{deployment_id}/restart", response_model=RestartResponse
)
async def restart_all_services(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
) -> RestartResponse:
    """Restart all services within a deployment."""
    try:
        # Get deployment from database
        deployment = await db.compose_deployments.afind_one(
            {
                "id": deployment_id,
                "user_id": current_user.user_id,
            }
        )

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Use WorkloadOperations to restart all services
        workload_ops = WorkloadOperations()
        result = workload_ops.restart_all_services(
            helm_values=deployment.helm_values, namespace=deployment.namespace
        )

        # Convert RestartResult objects to RestartServiceResult
        service_results: list[RestartServiceResult] = []
        service_names = [service.name for service in deployment.helm_values.services]
        for service_name, restart_result in zip(service_names, result.results):
            service_results.append(
                RestartServiceResult(
                    service=service_name,
                    success=restart_result.success,
                    message=restart_result.message,
                    resource_type=restart_result.resource_type,
                    resource_name=restart_result.resource_name,
                    output=restart_result.output,
                    error=restart_result.error,
                )
            )

        success = result.failed == 0
        if result.failed > 0:
            message = f"Restart completed with {result.failed} failures"
        else:
            message = f"Successfully restarted all {result.total_services} services"

        return RestartResponse(
            deployment_id=deployment_id,
            deployment_name=deployment.name,
            service_name=None,  # None indicates all services
            success=success,
            message=message,
            total_services=result.total_services,
            successful=result.successful,
            failed=result.failed,
            results=service_results,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to restart all services: {e}")
        raise HTTPException(status_code=500, detail="Failed to restart services")


@deployments_router.get(
    "/deployments/{deployment_id}/status", response_model=DeploymentStatusResponse
)
async def get_deployment_status(
    deployment_id: str,
    current_user: UserData = Depends(get_current_active_user),
) -> DeploymentStatusResponse:
    """Get resource status for a deployment."""
    # Get deployment from database
    deployment = await db.compose_deployments.afind_one(
        {
            "id": deployment_id,
            "user_id": current_user.user_id,
        }
    )

    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Use the K8sStatusWatcher to get current status
    from lazycloud_api.services.k8s.status_watcher import K8sStatusWatcher

    watcher = K8sStatusWatcher(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    # Get current status from watcher
    status_dict = await watcher.get_current_status()

    # Get service statuses
    services_list = []
    for service_config in deployment.helm_values.services:
        service_status = await watcher._get_service_status(service_config)
        services_list.append(service_status)

    # Determine overall deployment status
    all_ready = all(s.ready_replicas == s.replicas for s in services_list)
    any_running = any(s.ready_replicas > 0 for s in services_list)

    if all_ready:
        overall_status = KubernetesPhase.RUNNING
    elif any_running:
        overall_status = KubernetesPhase.PENDING  # partially running maps to pending
    else:
        overall_status = KubernetesPhase.STOPPED

    # Create deployment status
    deployment_status = DeploymentStatus(
        deployment_id=deployment.id,
        deployment_name=deployment.name,
        namespace=deployment.namespace,
        services=services_list,
        volumes=status_dict.get("volumes"),
        networks=status_dict.get("networks"),
        status=overall_status,
        ready=all_ready,
        last_updated=deployment.updated_at,
    )

    return DeploymentStatusResponse(status=deployment_status)

import yaml
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.security import (
    UserData,
    get_current_active_user,
)
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.prefect_app.compose import (
    deploy_compose_task,
    destroy_compose_task,
)
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from shared.models.deployments import DeploymentStates
from shared.models.statuses import TaskStatus
from shared.requests.deployments import (
    DeploymentCreateRequest,
)
from shared.responses.deployments import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
)
from shared.responses.tasks import DeploymentTaskStatusResponse

deployments_router = APIRouter(prefix="/deployments", tags=["deployments"])


@deployments_router.get("", response_model=DeploymentListResponse)
async def list_deployments(
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    deployment_id: str | None = None,
    name: str | None = None,
    current_user: UserData = Depends(get_current_active_user),
) -> DeploymentListResponse:
    """List compose deployments."""

    # append all search params if they are not None, always include user_id
    search_params = {"user_id": current_user.user_id}
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


@deployments_router.get(
    "/{deployment_id}/status", response_model=DeploymentStatusResponse
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

    watcher = StatusWatcher(
        deployment_id=deployment_id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
    )

    # Get deployment status directly from watcher
    deployment_status = await watcher.get_deployment_status()

    return DeploymentStatusResponse(status=deployment_status)


@deployments_router.post("", response_model=DeploymentTaskStatusResponse)
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


@deployments_router.delete(
    "/{deployment_id}", response_model=DeploymentTaskStatusResponse
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

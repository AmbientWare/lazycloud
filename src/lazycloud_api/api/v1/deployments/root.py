import yaml
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.dependencies import (
    get_deployment_with_access,
    get_deployment_with_admin_access,
    require_workspace_member,
)
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.user_workspaces import WorkspaceRole
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.prefect_app.compose import deploy_compose_task, destroy_compose_task
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from shared.models.deployments import DeploymentStates
from shared.models.statuses import TaskStatus
from shared.requests.deployments import DeploymentCreateRequest
from shared.responses.deployments import (
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
)
from shared.responses.tasks import DeploymentTaskStatusResponse

deployments_router = APIRouter(prefix="/deployments", tags=["deployments"])


@deployments_router.get("", response_model=DeploymentListResponse)
async def list_deployments(
    workspace_id: str,
    skip: int = 0,
    limit: int = 100,
    status: str | None = None,
    deployment_id: str | None = None,
    name: str | None = None,
    current_user: UserPydantic = Depends(get_current_active_user),
    _: None = Depends(require_workspace_member),
) -> DeploymentListResponse:
    """List compose deployments."""
    filters = {"workspace_id": workspace_id}
    if status:
        filters["status"] = status
    if deployment_id:
        filters["id"] = deployment_id
    if name:
        filters["name"] = name

    try:
        deployments = await db.compose_deployments.afind(filters=filters)
        total = len(deployments)
        paginated_deployments = deployments[skip : skip + limit]

        deployment_responses = [
            DeploymentResponse(
                id=deployment.id,
                workspace_id=str(deployment.workspace_id),
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
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
) -> DeploymentStatusResponse:
    """Get resource status for a deployment."""
    watcher = StatusWatcher(
        deployment_id=str(deployment.id),
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
    )

    deployment_status = await watcher.get_deployment_status()

    return DeploymentStatusResponse(status=deployment_status)


@deployments_router.post("", response_model=DeploymentTaskStatusResponse)
async def create_deployment(
    request: DeploymentCreateRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> DeploymentTaskStatusResponse:
    """Create a new compose deployment."""
    # make sure to check workspace permissions
    membership = await db.user_workspaces.aget_by_user_and_workspace(
        current_user.id, request.workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    # only owners and admins can create deployments
    if membership.role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    try:
        compose_data = yaml.safe_load(request.compose_yaml)
        namespace = create_ns_name(request.workspace_id)
        compose_file = ComposeParser.parse_dict(compose_data)

        validator = ComposeValidator()
        validation_result = validator.validate_to_result(compose_file)

        if not validation_result.can_deploy:
            raise ValueError(
                f"Validation errors: {'; '.join(validation_result.errors)}"
            )

        deployment = None
        if request.name:
            filters = {
                "workspace_id": request.workspace_id,
                "name": request.name,
            }
            deployment = await db.compose_deployments.afind_one(filters=filters)

        if deployment:
            # Block if deployment is in progress
            active_states = [
                DeploymentStates.PENDING,
                DeploymentStates.DEPLOYING,
                DeploymentStates.DELETING,
            ]
            if deployment.pending_compose_yaml and deployment.state in active_states:
                raise HTTPException(
                    409,
                    "Deployment is currently in progress. Please wait for it to complete.",
                )

            # Store new compose in pending_compose_yaml instead of overwriting
            deployment.pending_compose_yaml = request.compose_yaml
            deployment.state = DeploymentStates.PENDING
            deployment.status_message = "Update queued"
            deployment = await db.compose_deployments.aupdate(deployment)
        else:
            deployment_data = ComposeDeploymentPydantic(
                workspace_id=request.workspace_id,
                name=request.name,
                namespace=namespace,
                compose_yaml=request.compose_yaml,
                state=DeploymentStates.PENDING,
                status_message="Deployment queued",
            )
            deployment = await db.compose_deployments.acreate(deployment_data)

        if not deployment:
            raise HTTPException(status_code=500, detail="Deployment not found")

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
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
    _: UserPydantic = Depends(get_current_active_user),
) -> DeploymentTaskStatusResponse:
    """Delete a deployment."""
    try:
        await db.compose_deployments.update_status(
            str(deployment.id), DeploymentStates.DELETING, "Deletion initiated"
        )

        task_future = destroy_compose_task.delay(
            deployment_id=str(deployment.id),
        )

        return DeploymentTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message="Deletion task queued",
            deployment_id=str(deployment.id),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete deployment: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete deployment")

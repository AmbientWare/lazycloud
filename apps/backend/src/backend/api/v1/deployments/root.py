import uuid

from api_requests.deployments import (
    DeploymentCreateRequest,
    DeploymentRunRequest,
    RollbackRequest,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from models.deployments import DeploymentStates
from models.statuses import TaskStatus
from responses.deployments import (
    DeploymentHistoryResponse,
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
    Revision,
)
from responses.tasks import DeploymentTaskStatusResponse
from sqlalchemy.exc import IntegrityError

from backend.api.dependencies import (
    check_deployment_limit,
    get_deployment_with_access,
    get_deployment_with_admin_access,
    get_user_product_features,
    require_workspace_member,
)
from backend.api.security import get_current_active_user
from backend.billing.product_details.features import BaseFeatures
from backend.database import Database, get_db
from backend.database.compose import ComposeDeploymentPydantic
from backend.database.user_workspaces import WorkspaceRole
from backend.database.users import UserPydantic
from backend.prefect_app.client import run_flow
from backend.prefect_app.registry import Deployments
from backend.services.compose.validation import validate_deployment_request
from backend.services.k8s import create_ns_name, create_release_name
from backend.services.k8s.helm_manager import HelmManager
from backend.services.k8s.status_watcher import StatusWatcher

deployments_router = APIRouter(prefix="/deployments", tags=["deployments"])


@deployments_router.get("", response_model=DeploymentListResponse)
async def list_deployments(
    workspace_id: str,
    cursor: str | None = Query(None, description="Cursor to start from"),
    limit: int = Query(100, description="Limit the number of deployments returned"),
    status: str | None = Query(None, description="Status to filter by"),
    deployment_id: str | None = Query(None, description="Deployment ID to filter by"),
    name: str | None = Query(None, description="Name to filter by"),
    _: None = Depends(require_workspace_member),
    db: Database = Depends(get_db),
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
        offset = int(cursor) if cursor else 0
        total, deployments = await db.compose_deployments.find_paginated(
            filters=filters, offset=offset, limit=limit, include_deleted=False
        )

        deployment_responses = [
            DeploymentResponse(
                id=deployment.id,
                workspace_id=deployment.workspace_id,
                name=deployment.name,
                namespace=deployment.namespace,
                state=deployment.state,
                status_message=deployment.status_message,
                deployed_at=deployment.deployed_at,
                created_at=deployment.created_at,
                updated_at=deployment.updated_at,
            )
            for deployment in deployments
            if deployment.id is not None and deployment.name is not None
        ]

        return DeploymentListResponse(
            deployments=deployment_responses,
            total=total,
            cursor=str(offset + limit)
            if total is not None and total > offset + limit
            else None,
            limit=limit,
            has_more=total is not None and total > offset + limit,
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
        deployment_id=deployment.id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
        deployed_at=deployment.deployed_at,
    )

    deployment_status = await watcher.get_deployment_status()

    return DeploymentStatusResponse(status=deployment_status)


@deployments_router.post("", response_model=DeploymentResponse)
async def create_deployment(
    request: DeploymentCreateRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
    features: BaseFeatures = Depends(get_user_product_features),
    db: Database = Depends(get_db),
) -> DeploymentResponse:
    """Create or update a deployment record (does not trigger deployment)."""
    # make sure to check workspace permissions
    membership = await db.user_workspaces.get_by_user_and_workspace(
        current_user.id, request.workspace_id
    )
    if not membership:
        raise HTTPException(404, "Workspace not found")

    # only owners and admins can create deployments
    if membership.role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required")

    namespace = create_ns_name(request.workspace_id)

    # Check if this is an update to an existing deployment
    existing_deployment = None
    is_update = False
    if request.name:
        existing_deployment = await db.compose_deployments.get_by_name(
            workspace_id=request.workspace_id,
            name=request.name,
        )
        is_update = existing_deployment is not None

    # Only check deployment limit for new deployments (not updates)
    if not is_update:
        await check_deployment_limit(
            workspace_id=request.workspace_id,
            current_user=current_user,
            features=features,
            db=db,
        )

    # Create a temporary deployment object for basic validation
    temp_deployment = ComposeDeploymentPydantic(
        workspace_id=request.workspace_id,
        name=request.name or "",
        namespace=namespace,
        compose_yaml=request.compose_yaml,
        state=DeploymentStates.PENDING,
    )
    if existing_deployment:
        temp_deployment.id = str(existing_deployment.id)
    else:
        # Generate temporary ID for validation
        temp_deployment.id = str(uuid.uuid4())

    try:
        _, _, _ = await validate_deployment_request(
            temp_deployment, existing_deployment
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    except Exception as e:
        logger.exception(f"Validation failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to validate deployment configuration"
        ) from e

    deployment = None
    if request.name:
        try:
            deployment = await db.compose_deployments.find_one_with_lock(
                workspace_id=request.workspace_id, name=request.name
            )

            if deployment:
                # Block if actively deleting
                if deployment.state == DeploymentStates.DELETING:
                    raise HTTPException(
                        409,
                        "Deployment is currently being deleted. Please wait for it to complete.",
                    )

                deployment.pending_compose_yaml = request.compose_yaml
                # Reset to PENDING if updating from terminal state
                if deployment.state in (
                    DeploymentStates.DEPLOYED,
                    DeploymentStates.FAILED,
                    DeploymentStates.DELETED,
                ):
                    deployment.state = DeploymentStates.PENDING
                deployment.status_message = "Configuration updated"
                deployment = await db.compose_deployments.update(deployment)

            else:
                # New deployment: pending_compose_yaml, compose_yaml stays empty
                deployment_data = ComposeDeploymentPydantic(
                    workspace_id=request.workspace_id,
                    name=request.name,
                    namespace=namespace,
                    compose_yaml="",
                    pending_compose_yaml=request.compose_yaml,
                    state=DeploymentStates.PENDING,
                    status_message="Created",
                )
                deployment = await db.compose_deployments.create(deployment_data)

        except IntegrityError as e:
            if "uq_workspace_deployment_name" in str(e.orig):
                raise HTTPException(
                    409,
                    f"Deployment '{request.name}' already exists in this workspace. "
                    "Another request may have created it concurrently.",
                ) from e

            raise HTTPException(
                status_code=500,
                detail=f"Database constraint violation: {str(e)}",
            ) from e

        except HTTPException:
            raise

        except Exception as e:
            logger.error(f"Failed to create/update deployment in transaction: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to create deployment"
            ) from e

    else:
        try:
            deployment_data = ComposeDeploymentPydantic(
                workspace_id=request.workspace_id,
                name=request.name,
                namespace=namespace,
                compose_yaml="",
                pending_compose_yaml=request.compose_yaml,
                state=DeploymentStates.PENDING,
                status_message="Created",
            )
            deployment = await db.compose_deployments.create(deployment_data)

        except IntegrityError as e:
            if "uq_workspace_deployment_name" in str(e.orig):
                raise HTTPException(
                    409,
                    f"Deployment '{request.name}' already exists in this workspace. "
                    "Another request may have created it concurrently.",
                ) from e

            raise HTTPException(
                status_code=500,
                detail=f"Database constraint violation: {str(e)}",
            ) from e

        except Exception as e:
            logger.error(f"Failed to create deployment in transaction: {e}")
            raise HTTPException(
                status_code=500, detail="Failed to create deployment"
            ) from e

    if not deployment:
        raise HTTPException(status_code=500, detail="Deployment not found")

    return DeploymentResponse(
        id=deployment.id,
        workspace_id=deployment.workspace_id,
        name=deployment.name,
        namespace=deployment.namespace,
        state=deployment.state,
        status_message=deployment.status_message,
        deployed_at=deployment.deployed_at,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
    )


@deployments_router.post(
    "/{deployment_id}/deploy", response_model=DeploymentTaskStatusResponse
)
async def deploy_deployment(
    request: DeploymentRunRequest,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
    _: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> DeploymentTaskStatusResponse:
    """Trigger deployment of a deployment record."""
    # Block if actively deleting
    if deployment.state == DeploymentStates.DELETING:
        raise HTTPException(
            409,
            "Deployment is currently being deleted. Please wait for it to complete.",
        )

    # If compose_yaml provided in request, update it first
    if request.compose_yaml:
        deployment = await db.compose_deployments.get_by_id(
            deployment.id, with_lock=True
        )
        if deployment:
            deployment.pending_compose_yaml = request.compose_yaml
            # Reset to PENDING if updating from terminal state
            if deployment.state in (
                DeploymentStates.DEPLOYED,
                DeploymentStates.FAILED,
                DeploymentStates.DELETED,
            ):
                deployment.state = DeploymentStates.PENDING
            await db.compose_deployments.update(deployment)

    # Validate service-specific deployment requirements
    # compose file not set until deployment is created and migrated from pending_compose_yaml
    if request.service_names and not deployment.compose_yaml:
        services_str = ", ".join(request.service_names)
        raise HTTPException(
            400,
            f"Cannot deploy specific services '{services_str}': deployment has not been deployed yet. "
            "Deploy the full application first.",
        )

    # Get compose yaml to validate (use request if provided, else pending, else current)
    compose_yaml = (
        request.compose_yaml
        or deployment.pending_compose_yaml
        or deployment.compose_yaml
    )

    if not compose_yaml:
        raise HTTPException(
            400,
            "No compose configuration found. Create or update the deployment first.",
        )

    # Create temp deployment for validation with the compose yaml
    temp_deployment = ComposeDeploymentPydantic(
        workspace_id=deployment.workspace_id,
        name=deployment.name,
        namespace=deployment.namespace,
        compose_yaml=compose_yaml,
        state=deployment.state,
    )
    temp_deployment.id = str(deployment.id)

    # Full validation (compose parsing, helm generation, quotas)
    try:
        helm_values, _, _ = await validate_deployment_request(
            temp_deployment,
            deployment,
            service_names=request.service_names,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    except Exception as e:
        logger.exception(f"Validation failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to validate deployment configuration"
        ) from e

    # Update deployment with helm_values BEFORE triggering task
    # This prevents race condition where task checks its own status
    deployment = await db.compose_deployments.get_by_id(deployment.id, with_lock=True)
    if deployment:
        deployment.helm_values = helm_values
        await db.compose_deployments.update(deployment)

    # Trigger the deploy flow
    flow_run_id = await run_flow(
        Deployments.DEPLOY_COMPOSE,
        {
            "deployment_id": deployment.id,
            "wait_for_secrets": request.secrets,
            "service_names": request.service_names,
        },
    )

    # Update deployment with flow_run_id after flow is started
    # The flow's idempotency check handles the PENDING state correctly
    deployment = await db.compose_deployments.get_by_id(deployment.id, with_lock=True)
    if deployment:
        deployment.current_task_run_id = flow_run_id
        deployment.status_message = "Deployment task queued"
        await db.compose_deployments.update(deployment)

    return DeploymentTaskStatusResponse(
        task_id=flow_run_id,
        status=TaskStatus.PENDING,
        message="Deployment task queued",
        deployment_id=deployment.id,
    )


@deployments_router.delete(
    "/{deployment_id}", response_model=DeploymentTaskStatusResponse
)
async def delete_deployment(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
    _: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> DeploymentTaskStatusResponse:
    """Delete a deployment."""
    try:
        flow_run_id = await run_flow(
            Deployments.DESTROY_COMPOSE,
            {"deployment_id": deployment.id},
        )

        deployment = await db.compose_deployments.get_by_id(
            deployment.id, with_lock=True
        )
        if deployment:
            deployment.current_task_run_id = flow_run_id
            deployment.state = DeploymentStates.DELETING
            deployment.status_message = "Deletion initiated"
            await db.compose_deployments.update(deployment)

        return DeploymentTaskStatusResponse(
            task_id=flow_run_id,
            status=TaskStatus.PENDING,
            message="Deletion task queued",
            deployment_id=deployment.id,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to delete deployment: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete deployment")


@deployments_router.get(
    "/{deployment_id}/history", response_model=DeploymentHistoryResponse
)
async def get_deployment_history(
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_access),
) -> DeploymentHistoryResponse:
    """Get Helm release history for a deployment."""
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    if not name:
        raise HTTPException(status_code=400, detail="Deployment name is required")

    helm_manager = HelmManager()
    history = await helm_manager.get_history(name, namespace)

    revisions = [
        Revision(
            revision=item.get("revision", 0),
            status=item.get("status", "unknown"),
            chart=item.get("chart", ""),
            description=item.get("description", ""),
            updated=item.get("updated", ""),
        )
        for item in history
    ]

    return DeploymentHistoryResponse(revisions=revisions)


@deployments_router.post(
    "/{deployment_id}/rollback", response_model=DeploymentTaskStatusResponse
)
async def rollback_deployment(
    request: RollbackRequest,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
    _: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> DeploymentTaskStatusResponse:
    """Rollback a deployment to a previous Helm revision."""
    if deployment.state in (DeploymentStates.DELETING, DeploymentStates.DELETED):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot rollback deployment in state: {deployment.state}",
        )

    # Allow rollback from DEPLOYED, FAILED, or DEPLOYING states
    # DEPLOYING is allowed to handle stuck rollbacks (the task will validate state)
    if deployment.state not in (
        DeploymentStates.DEPLOYED,
        DeploymentStates.FAILED,
        DeploymentStates.DEPLOYING,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Deployment must be deployed, failed, or deploying to rollback (current state: {deployment.state})",
        )

    if request.revision <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Revision must be greater than 0 (got: {request.revision})",
        )

    flow_run_id = await run_flow(
        Deployments.ROLLBACK_COMPOSE,
        {
            "deployment_id": deployment.id,
            "revision": request.revision,
        },
    )

    deployment = await db.compose_deployments.get_by_id(deployment.id, with_lock=True)
    if deployment:
        deployment.current_task_run_id = flow_run_id
        deployment.state = DeploymentStates.DEPLOYING
        deployment.status_message = f"Rollback to revision {request.revision} queued"
        await db.compose_deployments.update(deployment)

    return DeploymentTaskStatusResponse(
        task_id=flow_run_id,
        status=TaskStatus.PENDING,
        message=f"Rollback to revision {request.revision} queued",
        deployment_id=deployment.id,
    )

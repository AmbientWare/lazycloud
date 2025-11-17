import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from sqlalchemy.exc import IntegrityError

from lazycloud_api.api.dependencies import (
    check_deployment_features,
    check_deployment_limit,
    get_deployment_with_access,
    get_deployment_with_admin_access,
    get_user_product_features,
    require_workspace_member,
)
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.billing.product_details.features import BaseFeatures
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.user_workspaces import WorkspaceRole
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.prefect_app.compose import (
    deploy_compose_task,
    destroy_compose_task,
    rollback_compose_task,
)
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name, create_release_name
from lazycloud_api.services.k8s.helm_manager import HelmManager
from lazycloud_api.services.k8s.status_watcher import StatusWatcher
from shared.models.deployments import DeploymentStates
from shared.models.statuses import TaskStatus
from shared.requests.deployments import DeploymentCreateRequest, RollbackRequest
from shared.responses.deployments import (
    DeploymentHistoryResponse,
    DeploymentListResponse,
    DeploymentResponse,
    DeploymentStatusResponse,
    Revision,
)
from shared.responses.tasks import DeploymentTaskStatusResponse

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
        total, deployments = await db.compose_deployments.afind_paginated(
            filters=filters, offset=offset, limit=limit, include_deleted=False
        )

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
        deployment_id=str(deployment.id),
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
        deployed_at=deployment.deployed_at,
    )

    deployment_status = await watcher.get_deployment_status()

    return DeploymentStatusResponse(status=deployment_status)


@deployments_router.post("", response_model=DeploymentTaskStatusResponse)
async def create_deployment(
    request: DeploymentCreateRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
    features: BaseFeatures = Depends(get_user_product_features),
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

        # Step 1: Check if this is an update to an existing deployment
        is_update = False
        if request.name:
            existing_deployment = await db.compose_deployments.aget_by_name(
                workspace_id=request.workspace_id,
                name=request.name,
            )
            is_update = existing_deployment is not None

        # Step 2: Only check deployment limit for new deployments (not updates)
        if not is_update:
            await check_deployment_limit(
                workspace_id=request.workspace_id,
                current_user=current_user,
                features=features,
            )

        # Step 3: Always check deployment features (services, volumes, networks, domains)
        # This ensures updates don't exceed limits even if they were previously within limits
        await check_deployment_features(
            compose_file=compose_file,
            compose_data=compose_data,
            features=features,
        )

        deployment = None
        if request.name:
            async with db.compose_deployments.transaction() as session:
                try:
                    deployment = await db.compose_deployments.afind_one_with_lock(
                        workspace_id=request.workspace_id,
                        name=request.name,
                        session=session,
                    )

                    if deployment:
                        # Block if there's a pending update queued (PENDING state)
                        # Block if actively deleting (DELETING state)
                        # Allow updates if DEPLOYING (might be stuck, allow recovery)
                        # Allow updates if terminal (DEPLOYED, FAILED, DELETED)
                        if (
                            deployment.pending_compose_yaml
                            and deployment.state == DeploymentStates.PENDING
                        ):
                            raise HTTPException(
                                409,
                                "Deployment is currently queued. Please wait for it to complete.",
                            )
                        if deployment.state == DeploymentStates.DELETING:
                            raise HTTPException(
                                409,
                                "Deployment is currently being deleted. Please wait for it to complete.",
                            )

                        deployment.pending_compose_yaml = request.compose_yaml
                        deployment.state = DeploymentStates.PENDING
                        deployment.status_message = "Update queued"
                        deployment = await db.compose_deployments.aupdate(
                            deployment, session=session
                        )
                    else:
                        deployment_data = ComposeDeploymentPydantic(
                            workspace_id=request.workspace_id,
                            name=request.name,
                            namespace=namespace,
                            compose_yaml=request.compose_yaml,
                            state=DeploymentStates.PENDING,
                            status_message="Deployment queued",
                        )
                        deployment = await db.compose_deployments.acreate(
                            deployment_data, session=session
                        )
                except IntegrityError as e:
                    await session.rollback()
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
                    await session.rollback()
                    raise
                except Exception as e:
                    await session.rollback()
                    logger.error(
                        f"Failed to create/update deployment in transaction: {e}"
                    )
                    raise HTTPException(
                        status_code=500, detail="Failed to create deployment"
                    ) from e
        else:
            deployment_data = ComposeDeploymentPydantic(
                workspace_id=request.workspace_id,
                name=request.name,
                namespace=namespace,
                compose_yaml=request.compose_yaml,
                state=DeploymentStates.PENDING,
                status_message="Deployment queued",
            )
            try:
                deployment = await db.compose_deployments.acreate(deployment_data)
            except IntegrityError as e:
                if "uq_workspace_deployment_name" in str(e.orig):
                    raise HTTPException(
                        409,
                        f"Deployment '{request.name}' already exists in this workspace. "
                        "Another request may have created it concurrently.",
                    ) from e
                raise HTTPException(
                    status_code=500, detail=f"Database constraint violation: {str(e)}"
                ) from e

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
        logger.error(f"Invalid YAML in deployment request: {e}")
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {str(e)}")
    except ValueError as e:
        logger.error(f"Validation error in deployment request: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except IntegrityError as e:
        logger.error(f"Database integrity error creating deployment: {e}")
        raise HTTPException(
            status_code=409,
            detail="Deployment already exists or constraint violation occurred",
        )
    except Exception as e:
        logger.error(
            f"Failed to create deployment for workspace {request.workspace_id}, "
            f"name {request.name}: {e}",
            exc_info=True,
        )
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
    history = helm_manager.get_history(name, namespace)

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

    task_future = rollback_compose_task.delay(
        deployment_id=deployment.id,
        revision=request.revision,
    )

    return DeploymentTaskStatusResponse(
        task_id=task_future.task_run_id,
        status=TaskStatus.PENDING,
        message=f"Rollback to revision {request.revision} queued",
        deployment_id=deployment.id,
    )

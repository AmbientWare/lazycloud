import yaml
from api_requests.deployments import (
    DeploymentCreateRequest,
    DeploymentRunRequest,
    RollbackRequest,
)
from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from models.billing import UsageUnits
from models.clusters import get_cluster_registry
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
    check_deployment_limit_for_owner,
    get_deployment_with_access,
    get_deployment_with_active_subscription,
    get_deployment_with_admin_access,
    get_features_for_owner,
    get_owner_with_active_subscription,
    require_workspace_member,
)
from backend.api.security import get_current_active_user
from backend.database import Database, get_db
from backend.database.models import (
    ComposeDeployment,
    ComposeDeploymentInDb,
    UserInDb,
    WorkspaceRole,
)
from backend.services.compose.parser import ComposeParser
from backend.services.compose.validation import validate_deployment_request
from backend.services.k8s import create_ns_name, create_release_name
from backend.services.k8s.client import is_cluster_available
from backend.services.k8s.generators.networking import compute_service_endpoints
from backend.services.k8s.helm_manager import HelmManager
from backend.services.k8s.status_watcher import StatusWatcher
from backend.services.storage_sizes import get_storage_sizes_cached
from backend.tasks.client import (
    run_deploy_compose,
    run_destroy_compose,
    run_rollback_compose,
)

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
                cluster_id=deployment.cluster_id,
            )
            for deployment in deployments
            if deployment.id is not None and deployment.name is not None
        ]

        return DeploymentListResponse(
            deployments=deployment_responses,
            total=total or 0,
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
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_access),
) -> DeploymentStatusResponse:
    """Get resource status for a deployment."""
    if not deployment.helm_values:
        raise HTTPException(400, "Deployment has no helm values")

    watcher = StatusWatcher(
        deployment_id=deployment.id,
        namespace=deployment.namespace,
        helm_values=deployment.helm_values,
        deployment_name=deployment.name,
        deployed_at=deployment.deployed_at,
        cluster_id=deployment.cluster_id,
    )

    deployment_status = await watcher.get_deployment_status()

    # Enrich volume summaries with storage sizes from billing data (cached)
    if deployment_status.volumes:
        storage_sizes = await get_storage_sizes_cached(deployment.id)
        for volume in deployment_status.volumes:
            if volume.name in storage_sizes:
                _, size_gb = storage_sizes[volume.name]
                volume.size = UsageUnits.format_size_gb(size_gb)

    return DeploymentStatusResponse(status=deployment_status)


@deployments_router.post("", response_model=DeploymentResponse)
async def create_deployment(
    request: DeploymentCreateRequest,
    current_user: UserInDb = Depends(get_current_active_user),
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

    # Get cluster for new deployment (uses default cluster for now)
    registry = get_cluster_registry()
    cluster = registry.get_cluster_for_placement()
    if not cluster:
        raise HTTPException(503, "No available clusters for deployment")

    # Verify the cluster has an available K8s client (guards against failed client loading)
    if not is_cluster_available(cluster.name):
        logger.error(f"Cluster {cluster.name} has no available K8s client")
        raise HTTPException(503, f"Cluster {cluster.name} is not available")

    target_cluster_id = cluster.name

    # Check if this is an update to an existing deployment
    existing_deployment = None
    is_update = False
    if request.name:
        existing_deployment = await db.compose_deployments.get_by_name(
            workspace_id=request.workspace_id,
            name=request.name,
        )
        is_update = existing_deployment is not None

    # Get workspace owner and verify active subscription
    owner_user = await get_owner_with_active_subscription(
        workspace_id=request.workspace_id,
        current_user=current_user,
        db=db,
    )

    # Only check deployment limit for new deployments (not updates)
    if not is_update:
        await check_deployment_limit_for_owner(owner_user)

    # Create a temporary deployment object for basic validation
    # For updates, keep the deployment on its existing cluster
    validation_cluster_id = (
        existing_deployment.cluster_id if existing_deployment else target_cluster_id
    )
    temp_deployment = ComposeDeployment(
        workspace_id=request.workspace_id,
        name=request.name or "",
        namespace=namespace,
        compose_yaml=request.compose_yaml,
        state=DeploymentStates.PENDING,
        cluster_id=validation_cluster_id,
    )

    try:
        deployment_id = existing_deployment.id if existing_deployment else None
        _, _ = await validate_deployment_request(
            deployment_id, temp_deployment, existing_deployment
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
                deployment_data = ComposeDeployment(
                    workspace_id=request.workspace_id,
                    name=request.name,
                    namespace=namespace,
                    compose_yaml="",
                    pending_compose_yaml=request.compose_yaml,
                    state=DeploymentStates.PENDING,
                    status_message="Created",
                    cluster_id=target_cluster_id,
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
            deployment_data = ComposeDeployment(
                workspace_id=request.workspace_id,
                name=request.name or "tmp-deployment",
                namespace=namespace,
                compose_yaml="",
                pending_compose_yaml=request.compose_yaml,
                state=DeploymentStates.PENDING,
                status_message="Created",
                cluster_id=target_cluster_id,
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

    # Compute service endpoints from the compose yaml
    endpoints = None
    compose_yaml_to_parse = request.compose_yaml or deployment.compose_yaml
    if compose_yaml_to_parse:
        try:
            compose_data = yaml.safe_load(compose_yaml_to_parse)
            compose_file = ComposeParser.parse_dict(compose_data)
            endpoints = compute_service_endpoints(
                compose_file, deployment.id, deployment.cluster_id
            )
        except Exception as e:
            logger.warning(f"Failed to compute endpoints: {e}")

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
        cluster_id=deployment.cluster_id,
        endpoints=endpoints,
    )


@deployments_router.post(
    "/{deployment_id}/deploy", response_model=DeploymentTaskStatusResponse
)
async def deploy_deployment(
    request: DeploymentRunRequest,
    deployment: ComposeDeploymentInDb = Depends(
        get_deployment_with_active_subscription
    ),
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
    if request.compose_yaml is not None:
        deployment.pending_compose_yaml = request.compose_yaml
        # Reset to PENDING if updating from terminal state
        if deployment.state in (
            DeploymentStates.DEPLOYED,
            DeploymentStates.FAILED,
            DeploymentStates.DELETED,
        ):
            deployment.state = DeploymentStates.PENDING
        updated_deployment = await db.compose_deployments.update(deployment)
        if updated_deployment:
            deployment = updated_deployment

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
    temp_deployment = ComposeDeployment(
        workspace_id=deployment.workspace_id,
        name=deployment.name,
        namespace=deployment.namespace,
        compose_yaml=compose_yaml,
        state=deployment.state,
        depot_project_id=deployment.depot_project_id,
        cluster_id=deployment.cluster_id,
    )

    # Get owner features for helm values generation (instance class selection)
    owner_user = await db.workspaces.get_owner_user(deployment.workspace_id)
    features = await get_features_for_owner(owner_user) if owner_user else None

    # Full validation (compose parsing, helm generation, quotas)
    try:
        helm_values, _ = await validate_deployment_request(
            deployment_id=deployment.id,
            deployment=temp_deployment,
            existing_deployment=deployment,
            service_names=request.service_names,
            features=features,
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    except Exception as e:
        logger.exception(f"Validation failed unexpectedly: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to validate deployment configuration"
        ) from e

    deployment.helm_values = helm_values
    updated_deployment = await db.compose_deployments.update(deployment)
    if updated_deployment:
        deployment = updated_deployment

    # Trigger the deploy job
    job_key = await run_deploy_compose(
        deployment_id=deployment.id,
        wait_for_secrets=request.secrets,
        service_names=request.service_names,
    )

    # Update deployment with job_key after job is enqueued
    # The job's idempotency check handles the PENDING state correctly
    deployment.current_task_run_id = job_key
    deployment.status_message = "Deployment task queued"
    updated_deployment = await db.compose_deployments.update(deployment)
    if updated_deployment:
        deployment = updated_deployment

    return DeploymentTaskStatusResponse(
        task_id=job_key,
        status=TaskStatus.PENDING,
        message="Deployment task queued",
        deployment_id=deployment.id,
    )


@deployments_router.delete(
    "/{deployment_id}", response_model=DeploymentTaskStatusResponse
)
async def delete_deployment(
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
    db: Database = Depends(get_db),
) -> DeploymentTaskStatusResponse:
    """Delete a deployment."""
    try:
        job_key = await run_destroy_compose(deployment_id=deployment.id)

        if deployment:
            deployment.current_task_run_id = job_key
            deployment.state = DeploymentStates.DELETING
            deployment.status_message = "Deletion initiated"
            await db.compose_deployments.update(deployment)

        return DeploymentTaskStatusResponse(
            task_id=job_key,
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
    deployment: ComposeDeployment = Depends(get_deployment_with_access),
) -> DeploymentHistoryResponse:
    """Get Helm release history for a deployment."""
    name = create_release_name(deployment.workspace_id, deployment.name)
    namespace = deployment.namespace

    if not name:
        raise HTTPException(status_code=400, detail="Deployment name is required")

    helm_manager = HelmManager(deployment.cluster_id)
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
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
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

    job_key = await run_rollback_compose(
        deployment_id=deployment.id,
        revision=request.revision,
    )

    deployment.current_task_run_id = job_key
    deployment.state = DeploymentStates.DEPLOYING
    deployment.status_message = f"Rollback to revision {request.revision} queued"
    updated_deployment = await db.compose_deployments.update(deployment)
    if updated_deployment:
        deployment = updated_deployment

    return DeploymentTaskStatusResponse(
        task_id=job_key,
        status=TaskStatus.PENDING,
        message=f"Rollback to revision {request.revision} queued",
        deployment_id=deployment.id,
    )

import asyncio

import yaml
from api_requests.deployments import DiffRequest, DiffType
from fastapi import APIRouter, Body, Depends, HTTPException
from loguru import logger
from models.clusters import get_cluster_registry
from models.deployments import DeploymentStates
from models.diffs import EnvVarChanges, StorageTypeChange
from models.secrets import SecretSource
from responses.deployments import DiffResponse

from backend.api.dependencies import require_workspace_admin
from backend.api.security import get_current_active_user
from backend.database import Database, get_db
from backend.database.models import ComposeDeploymentPydantic, UserPydantic
from backend.services.compose.diff_checker import (
    ComposeDiffChecker,
    detect_storage_type_changes,
)
from backend.services.compose.parser import ComposeParser
from backend.services.compose.validation import validate_deployment_request
from backend.services.k8s import create_ns_name
from backend.services.k8s.client import get_namespace_pvcs
from backend.services.k8s.generators.networking import compute_service_endpoints

diff_router = APIRouter(prefix="/diff")


async def _get_deployment_or_verify_workspace(
    request: DiffRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
    db: Database = Depends(get_db),
):
    """Get deployment by name for existing, verify workspace access for new."""
    # Verify workspace access first
    await require_workspace_admin(request.workspace_id, current_user, db=db)

    if request.diff_type == DiffType.NEW:
        return None

    deployment = await db.compose_deployments.get_by_name(
        request.workspace_id, request.deployment_name
    )

    if not deployment:
        raise HTTPException(
            status_code=404,
            detail=f"Deployment '{request.deployment_name}' not found in workspace",
        )

    return deployment


@diff_router.post("", response_model=DiffResponse)
async def get_deployment_diff(
    request: DiffRequest = Body(...),
    deployment: ComposeDeploymentPydantic | None = Depends(
        _get_deployment_or_verify_workspace
    ),
    db: Database = Depends(get_db),
) -> DiffResponse:
    """Compare current deployment with proposed changes."""

    # Parse new compose file
    compose_data = yaml.safe_load(request.compose_yaml)
    logger.info(
        f"Diff endpoint received compose_yaml with services: {list(compose_data.get('services', {}).keys())}"
    )
    for svc_name, svc_config in compose_data.get("services", {}).items():
        logger.info(
            f"  Service '{svc_name}': image={svc_config.get('image')}, build={svc_config.get('build')}"
        )
    compose_file = ComposeParser.parse_dict(compose_data)

    # Determine workspace_id and namespace
    workspace_id = deployment.workspace_id if deployment else request.workspace_id
    namespace = create_ns_name(workspace_id)

    # Handle environment variable diff
    env_var_changes = None
    if request.diff_type == DiffType.EXISTING:
        # Existing deployment - compare with current secrets
        existing_secrets = await db.secrets.get_secrets(deployment.id)

        existing_keys, user_managed_keys = set(), set()
        for secret in existing_secrets:
            if secret.source == SecretSource.COMPOSE:
                existing_keys.add(secret.key)
            elif secret.source == SecretSource.USER:
                user_managed_keys.add(secret.key)

        new_keys = set(request.env_keys)

        env_var_changes = EnvVarChanges(
            added=sorted(list(new_keys - existing_keys)),
            removed=sorted(list(existing_keys - new_keys)),
            existing=sorted(list(new_keys & existing_keys)),
            user_managed=sorted(list(user_managed_keys)),
        )
    else:
        # New deployment - all keys are "added"
        env_var_changes = EnvVarChanges(
            added=sorted(request.env_keys), removed=[], existing=[], user_managed=[]
        )

    # Get current compose file for comparison
    # Only parse if compose_yaml has content (empty means nothing deployed yet)
    current_compose = None
    if deployment and deployment.compose_yaml:
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

    # Perform full validation (Helm generation, quota checks, compose validation)
    can_deploy = True
    full_validation_errors = []
    warnings = []
    try:
        # Determine cluster_id: use existing deployment's cluster or get from placement
        if deployment:
            cluster_id = deployment.cluster_id
        else:
            registry = get_cluster_registry()
            cluster = registry.get_cluster_for_placement()
            cluster_id = cluster.name if cluster else "default"

        # Create temporary deployment for full validation
        temp_deployment = ComposeDeploymentPydantic(
            workspace_id=workspace_id,
            name=request.deployment_name or "",
            namespace=namespace,
            compose_yaml=request.compose_yaml,
            state=DeploymentStates.PENDING,
            cluster_id=cluster_id,
        )

        if deployment:
            # Ensure id is string (direct assignment bypasses Pydantic validators)
            temp_deployment.id = str(deployment.id)
        else:
            # For new deployments, don't set id to avoid unnecessary DB secret lookup
            temp_deployment.id = None

        # Run full validation (includes compose validation via HelmValuesGenerator)
        _, warnings = await validate_deployment_request(temp_deployment, deployment)

    except ValueError as e:
        # User-friendly validation errors
        logger.exception(f"Validation ValueError: {e}")
        full_validation_errors = [str(e)]
        can_deploy = False

    except Exception as e:
        # Unexpected errors
        logger.exception(f"Full validation failed unexpectedly: {e}")
        full_validation_errors = [f"Validation failed: {str(e)}"]
        can_deploy = False

    all_errors = full_validation_errors

    # Detect storage type changes (EBS ↔ EFS transitions)
    storage_type_changes: list[StorageTypeChange] | None = None
    if deployment and request.diff_type == DiffType.EXISTING:
        try:
            existing_pvcs = await asyncio.wait_for(
                get_namespace_pvcs(namespace, cluster_id),
                timeout=3.0,
            )
            if existing_pvcs:
                changes = detect_storage_type_changes(compose_file, existing_pvcs)
                if changes:
                    storage_type_changes = changes
        except asyncio.TimeoutError:
            logger.warning(
                f"Timed out detecting storage type changes for namespace {namespace}"
            )
            warnings.append(
                "Could not detect storage type changes: Kubernetes API timeout"
            )
        except Exception as e:
            logger.warning(f"Failed to detect storage type changes: {e}")
            warnings.append(f"Could not detect storage type changes: {str(e)}")

    # Only return existing_compose_yaml if it has content (empty means nothing deployed yet)
    existing_yaml = None
    if deployment and deployment.compose_yaml:
        existing_yaml = deployment.compose_yaml

    # Compute service endpoints
    # For existing deployments, use the real ID; for new, use None (will show placeholder)
    deployment_id_for_endpoints = deployment.id if deployment else None
    endpoints = compute_service_endpoints(
        compose_file, deployment_id_for_endpoints, cluster_id
    )

    return DiffResponse(
        deployment_id=str(deployment.id) if deployment else "new",
        namespace=namespace,
        has_changes=compose_diff.has_changes(),
        diff=compose_diff,
        env_var_changes=env_var_changes,
        storage_type_changes=storage_type_changes,
        errors=all_errors if all_errors else None,
        warnings=warnings,
        can_deploy=can_deploy,
        existing_compose_yaml=existing_yaml,
        endpoints=endpoints,
    )

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.dependencies import require_workspace_admin
from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services.compose.diff_checker import ComposeDiffChecker
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name
from shared.models.diffs import EnvVarChanges
from shared.models.secrets import SecretSource
from shared.requests.deployments import DiffRequest, DiffType
from shared.responses.deployments import DiffResponse

diff_router = APIRouter(prefix="/diff")


async def _get_deployment_or_verify_workspace(
    request: DiffRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
):
    """Get deployment by name for existing, verify workspace access for new."""
    # Verify workspace access first
    await require_workspace_admin(request.workspace_id, current_user)

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
) -> DiffResponse:
    """Compare current deployment with proposed changes."""

    # Parse new compose file
    compose_data = yaml.safe_load(request.compose_yaml)
    compose_file = ComposeParser.parse_dict(compose_data)

    # Validate the compose file
    validator = ComposeValidator()
    validation_result = validator.validate_to_result(compose_file)

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
        deployment_id=deployment.id if deployment else "new",
        namespace=namespace,
        has_changes=compose_diff.has_changes(),
        diff=compose_diff,
        env_var_changes=env_var_changes,
        errors=validation_result.errors,
        warnings=validation_result.warnings,
    )

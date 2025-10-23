import yaml
from fastapi import APIRouter, Body, Depends
from loguru import logger

from lazycloud_api.api.dependencies import (
    get_current_active_user,
    get_deployment_with_admin_access,
    require_workspace_admin,
)
from lazycloud_api.database import db
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services.compose.diff_checker import ComposeDiffChecker
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name
from shared.models.diffs import EnvVarChanges
from shared.requests.deployments import DiffRequest
from shared.responses.deployments import DiffResponse

diff_router = APIRouter(prefix="/diff")


async def _new_deployment_or_check_workspace(
    deployment_id: str,
    request: DiffRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
):
    """Get deployment for existing, verify workspace access for new."""
    if deployment_id == "new":
        await require_workspace_admin(request.workspace_id, current_user)
        return None

    return await get_deployment_with_admin_access(deployment_id, current_user)


@diff_router.post("", response_model=DiffResponse)
async def get_deployment_diff(
    request: DiffRequest = Body(...),
    deployment=Depends(_new_deployment_or_check_workspace),
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
    if deployment and deployment.name != "new":
        # Get existing secrets from database
        existing_secret = await db.secrets.aget_secret(deployment.id)
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
        deployment_id=deployment.id if deployment else "new",
        namespace=namespace,
        has_changes=compose_diff.has_changes(),
        diff=compose_diff,
        env_var_changes=env_var_changes,
        errors=validation_result.errors,
        warnings=validation_result.warnings,
    )

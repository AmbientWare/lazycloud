import yaml
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.security import get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.database.users import UserPydantic
from lazycloud_api.services.compose.diff_checker import ComposeDiffChecker
from lazycloud_api.services.compose.parser import ComposeParser
from lazycloud_api.services.compose.validator import ComposeValidator
from lazycloud_api.services.k8s import create_ns_name
from shared.models.diffs import EnvVarChanges
from shared.requests.deployments import DiffRequest
from shared.responses.deployments import DiffResponse

diff_router = APIRouter(prefix="/diff", tags=["diff"])


@diff_router.post("/{deployment_id}", response_model=DiffResponse)
async def get_deployment_diff(
    deployment_id: str,
    request: DiffRequest,
    current_user: UserPydantic = Depends(get_current_active_user),
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
        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

    # Parse new compose file
    compose_data = yaml.safe_load(request.compose_yaml)
    compose_file = ComposeParser.parse_dict(compose_data)

    # Validate the compose file
    validator = ComposeValidator()
    validation_result = validator.validate_to_result(compose_file)

    # Determine namespace
    namespace = create_ns_name(current_user.id)

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

from fastapi import APIRouter, Depends, HTTPException, Query

from lazycloud_api.api.dependencies import get_deployment_with_admin_access
from lazycloud_api.api.security import get_current_active_user, require_admin
from lazycloud_api.database import db
from lazycloud_api.database.compose import ComposeDeploymentPydantic
from lazycloud_api.database.secrets import SecretPydantic
from lazycloud_api.database.user_workspaces import WorkspaceRole
from lazycloud_api.database.users import UserPydantic
from shared.requests.secrets import SecretsRequest
from shared.responses.secrets import SecretsResponse, SecretsStoredResponse

secrets_router = APIRouter(prefix="/secrets", dependencies=[Depends(require_admin)])


async def _require_admin_for_secret_values(
    deployment_id: str,
    show_values: bool = False,
    current_user: UserPydantic = Depends(get_current_active_user),
) -> ComposeDeploymentPydantic:
    """Verify user has appropriate access to deployment secrets."""
    deployment, role = await db.compose_deployments.aget_with_workspace_access(
        deployment_id, current_user.id
    )

    if not deployment or not role:
        raise HTTPException(404, "Deployment not found")

    # Require admin access to view secret values
    if show_values and role not in [WorkspaceRole.OWNER, WorkspaceRole.ADMIN]:
        raise HTTPException(403, "Admin or owner role required to view secret values")

    return deployment


@secrets_router.get("")
async def get_secrets(
    show_values: bool = Query(
        default=False, description="Show actual secret values (requires admin)"
    ),
    deployment: ComposeDeploymentPydantic = Depends(_require_admin_for_secret_values),
) -> SecretsResponse:
    """Get secrets for a deployment"""
    secrets = await db.secrets.aget_secret(deployment.id)
    if not secrets:
        raise HTTPException(404, "Secrets not found")

    # Mask values unless user requested and has permission to see them
    masked_secrets = (
        secrets.secrets
        if show_values
        else {key: "● ● ● ● ● ● ● ●" for key in secrets.secrets.keys()}
    )

    return SecretsResponse(secrets=masked_secrets)


@secrets_router.get("/value/{key}")
async def get_secret_value(
    key: str,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
) -> str:
    """Get a secret value for a deployment."""
    secrets = await db.secrets.aget_secret(deployment.id)
    if not secrets:
        raise HTTPException(status_code=404, detail="Secrets not found")

    return str(secrets.secrets.get(key, ""))


@secrets_router.post("")
async def store_secrets(
    request: SecretsRequest,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
) -> SecretsStoredResponse:
    """Create secrets for a deployment"""
    # Check if secrets already exist - post should fail if they do
    current_secrets = await db.secrets.aget_secret(deployment.id)
    if current_secrets:
        raise HTTPException(
            status_code=409,
            detail="Secrets already exist for this deployment. Use PATCH to update.",
        )

    to_store = request.secrets_collection.added or {}
    secret_data = {}

    for key, value in to_store.items():
        secret_data[key] = value

    # Create new secrets
    await db.secrets.aupdate_or_create(
        SecretPydantic(
            deployment_id=deployment.id,
            secrets=secret_data,
        )
    )

    return SecretsStoredResponse(
        deployment_id=deployment.id, secrets_count=len(secret_data)
    )


@secrets_router.patch("")
async def patch_secrets(
    request: SecretsRequest,
    deployment: ComposeDeploymentPydantic = Depends(get_deployment_with_admin_access),
) -> SecretsStoredResponse:
    """Partially update secrets for a deployment. Fails if secrets don't exist."""
    to_store = request.secrets_collection.added or {}
    to_remove = request.secrets_collection.removed or []

    # Check if secrets exist - patch should fail if they don't
    current_secrets = await db.secrets.aget_secret(deployment.id)
    if not current_secrets:
        raise HTTPException(
            status_code=404,
            detail="Secrets not found for deployment. Use POST to create.",
        )

    secret_data = current_secrets.secrets

    # add or update secrets
    for key, value in to_store.items():
        secret_data[key] = value

    # remove secrets
    for key in to_remove:
        if key in secret_data:
            del secret_data[key]

    # Update existing secrets
    await db.secrets.aupdate_or_create(
        SecretPydantic(
            deployment_id=deployment.id,
            secrets=secret_data,
        )
    )

    return SecretsStoredResponse(
        deployment_id=deployment.id, secrets_count=len(secret_data)
    )

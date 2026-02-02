from api_requests.secrets import SecretsRequest
from fastapi import APIRouter, Depends, HTTPException, Query
from models.secrets import BasicSecret, SecretState
from responses.secrets import SecretsResponse, SecretsStoredResponse

from backend.api.dependencies import get_deployment_with_admin_access
from backend.api.security import get_current_active_user, require_admin
from backend.database import Database, get_db
from backend.database.models import (
    ComposeDeploymentInDb,
    Secret,
    UserInDb,
    WorkspaceRole,
)

secrets_router = APIRouter(
    prefix="/{deployment_id}/secrets", dependencies=[Depends(require_admin)]
)


async def _require_admin_for_secret_values(
    deployment_id: str,
    show_values: bool = False,
    current_user: UserInDb = Depends(get_current_active_user),
    db: Database = Depends(get_db),
) -> ComposeDeploymentInDb:
    """Verify user has appropriate access to deployment secrets."""
    deployment, role = await db.compose_deployments.get_with_workspace_access(
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
    deployment: ComposeDeploymentInDb = Depends(_require_admin_for_secret_values),
    db: Database = Depends(get_db),
) -> SecretsResponse:
    """Get secrets for a deployment"""
    secrets = await db.secrets.get_secrets(deployment.id)

    response_secrets = [
        BasicSecret(
            key=secret.key,
            value=secret.value if show_values else "● ● ● ● ● ● ● ●",
            source=secret.source,
            state=secret.state,
        )
        for secret in secrets
    ]

    return SecretsResponse(secrets=response_secrets)


@secrets_router.get("/value/{key}")
async def get_secret_value(
    key: str,
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
    db: Database = Depends(get_db),
) -> str:
    """Get a secret value for a deployment."""
    secret = await db.secrets.get_secret_by_key(deployment.id, key)
    if not secret:
        raise HTTPException(status_code=404, detail=f"Secret '{key}' not found")

    return secret.value


@secrets_router.post("")
async def store_secrets(
    request: SecretsRequest,
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
    db: Database = Depends(get_db),
) -> SecretsStoredResponse:
    """Create secrets for a deployment"""
    secrets = request.secrets

    if not secrets:
        return SecretsStoredResponse(deployment_id=deployment.id, secrets_count=0)

    # Check if any secrets already exist (single query)
    existing_secrets = await db.secrets.get_secrets(deployment.id)
    existing_keys = {secret.key for secret in existing_secrets}

    # Find duplicates
    new_keys = [secret.key for secret in secrets]
    duplicates = existing_keys.intersection(new_keys)
    if duplicates:
        raise HTTPException(
            status_code=409,
            detail=f"Secrets already exist: {', '.join(sorted(duplicates))}. Use PATCH to update or remove them.",
        )

    # Bulk create all secrets
    secrets_to_create = [
        Secret(
            deployment_id=deployment.id,
            key=secret.key,
            value=secret.value,
            source=secret.source,
            state=SecretState.AWAITING_DEPLOYMENT,
        )
        for secret in secrets
    ]

    await db.secrets.create_bulk(secrets_to_create)

    return SecretsStoredResponse(
        deployment_id=deployment.id, secrets_count=len(secrets_to_create)
    )


@secrets_router.patch("")
async def update_secrets(
    request: SecretsRequest,
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
    db: Database = Depends(get_db),
) -> SecretsStoredResponse:
    """Update existing secrets for a deployment. Does not create new secrets."""
    secrets = request.secrets

    # Update existing secrets only (fail if not found)
    for secret in secrets:
        updated = await db.secrets.update_by_key(
            deployment_id=deployment.id,
            key=secret.key,
            value=secret.value,
            source=secret.source,
            state=SecretState.AWAITING_DEPLOYMENT,
        )
        if not updated:
            raise HTTPException(
                status_code=404,
                detail=f"Secret '{secret.key}' not found. Use POST to create new secrets.",
            )

    # Get final count of secrets
    remaining_secrets = await db.secrets.get_secrets(deployment.id)

    return SecretsStoredResponse(
        deployment_id=deployment.id, secrets_count=len(remaining_secrets)
    )


@secrets_router.delete("")
async def delete_secrets(
    request: SecretsRequest,
    deployment: ComposeDeploymentInDb = Depends(get_deployment_with_admin_access),
    db: Database = Depends(get_db),
) -> SecretsStoredResponse:
    """Delete existing secrets for a deployment"""
    secrets = request.secrets

    if not secrets:
        return SecretsStoredResponse(deployment_id=deployment.id, secrets_count=0)

    # Delete existing secrets
    for secret in secrets:
        deleted = await db.secrets.delete_secret_by_key(deployment.id, secret.key)
        if not deleted:
            raise HTTPException(
                status_code=404,
                detail=f"Secret '{secret.key}' not found.",
            )

    return SecretsStoredResponse(deployment_id=deployment.id, secrets_count=0)

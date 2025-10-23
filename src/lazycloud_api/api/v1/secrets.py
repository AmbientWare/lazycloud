from fastapi import APIRouter, Depends, HTTPException, Query

from lazycloud_api.api.security import require_admin
from lazycloud_api.database import db
from lazycloud_api.database.secrets import SecretPydantic
from shared.requests.secrets import SecretsRequest
from shared.responses.secrets import SecretsResponse, SecretsStoredResponse

secrets_router = APIRouter(
    prefix="/secrets", tags=["secrets"], dependencies=[Depends(require_admin)]
)


@secrets_router.get("/{deployment_id}")
async def get_secrets(
    deployment_id: str, show_values: bool = Query(default=False)
) -> SecretsResponse:
    """Get secrets for a deployment."""
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    secrets = await db.secrets.aget_secret(deployment_id)
    if not secrets:
        raise HTTPException(status_code=404, detail="Secrets not found")

    return SecretsResponse(
        secrets=secrets.secrets
        if show_values
        else {key: "● ● ● ● ● ● ● ●" for key in secrets.secrets.keys()}
    )


@secrets_router.get("/{deployment_id}/value/{key}")
async def get_secret_value(deployment_id: str, key: str) -> str:
    """Get a secret value for a deployment."""
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    secrets = await db.secrets.aget_secret(deployment_id)
    if not secrets:
        raise HTTPException(status_code=404, detail="Secrets not found")

    return str(secrets.secrets.get(key, ""))


@secrets_router.post("/{deployment_id}")
async def store_secrets(
    deployment_id: str,
    request: SecretsRequest,
) -> SecretsStoredResponse:
    """Create secrets for a deployment"""
    # Check if deployment exists
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    # Check if secrets already exist - post should fail if they do
    current_secrets = await db.secrets.aget_secret(deployment_id)
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
            deployment_id=str(deployment.id),
            secrets=secret_data,
        )
    )

    return SecretsStoredResponse(
        deployment_id=str(deployment.id), secrets_count=len(secret_data)
    )


@secrets_router.patch("/{deployment_id}")
async def patch_secrets(
    deployment_id: str, request: SecretsRequest
) -> SecretsStoredResponse:
    """Partially update secrets for a deployment. Fails if secrets don't exist."""
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    to_store = request.secrets_collection.added or {}
    to_remove = request.secrets_collection.removed or []

    # Check if secrets exist - patch should fail if they don't
    current_secrets = await db.secrets.aget_secret(deployment_id)
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
            deployment_id=str(deployment.id),
            secrets=secret_data,
        )
    )

    return SecretsStoredResponse(
        deployment_id=str(deployment.id), secrets_count=len(secret_data)
    )

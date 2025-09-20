from fastapi import APIRouter, Depends, HTTPException

from lazycloud_api.api.security import require_admin
from lazycloud_api.database import db
from lazycloud_api.database.secrets import SecretPydantic
from shared.requests.secrets import SecretsRequest
from shared.responses.secrets import SecretsStoredResponse

secrets_router = APIRouter(
    prefix="/secrets", tags=["secrets"], dependencies=[Depends(require_admin)]
)


@secrets_router.post("/{deployment_id}")
async def store_secrets(
    deployment_id: str,
    request: SecretsRequest,
) -> SecretsStoredResponse:
    """Store secrets for a deployment."""
    # Check if deployment exists
    deployment = await db.compose_deployments.aget_by_id(deployment_id)
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    to_store = request.secrets_collection.added or {}
    to_remove = request.secrets_collection.removed or []

    # check if secrets are already stored
    current_secrets = await db.secrets.aget_secret(deployment_id)
    if current_secrets:
        secret_data = current_secrets.secrets
    else:
        secret_data = {}

    for key, value in to_store.items():
        secret_data[key] = value

    for key in to_remove:
        if key in secret_data:
            del secret_data[key]

    # Use update_or_create which handles encryption
    await db.secrets.aupdate_or_create(
        SecretPydantic(
            deployment_id=str(deployment.id),
            secrets=secret_data,
            user_id=deployment.user_id,
        )
    )

    return SecretsStoredResponse(
        deployment_id=str(deployment.id), secrets_count=len(secret_data)
    )

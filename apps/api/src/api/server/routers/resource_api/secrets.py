from __future__ import annotations

from control.storage_relationships import StorageRelationshipService
from fastapi import APIRouter, Depends, status
from shared.http.secrets import (
    CreateSecretResponse,
    GetSecretResponse,
    ListSecretsResponse,
    SecretDeleteResponse,
    SecretMaskedListResponse,
    SecretMaskedRecord,
    SecretMaskedSetResponse,
    SecretPayload,
    SecretValuePayload,
    SecretWireRecord,
    UpdateSecretResponse,
)

from api.server.auth import read_workspace, write_workspace
from api.server.dependencies import current_services
from api.server.services import ApiServices

router = APIRouter()


@router.get("/api/v1/secrets", response_model=SecretMaskedListResponse, operation_id="list_secrets")
def list_secrets_masked(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> SecretMaskedListResponse:
    relationships = StorageRelationshipService(services.deployment_resources).for_workspace(
        workspace_id
    )
    return SecretMaskedListResponse(
        secrets=[
            SecretMaskedRecord(
                name=item.name,
                value=item.masked(),
                created_at=item.created_at,
                updated_at=item.updated_at,
                workloads=list(relationships.secrets.get(item.name, ())),
            )
            for item in services.secrets.list(workspace=workspace_id)
        ]
    )


@router.get(
    "/api/v1/secrets/full",
    response_model=ListSecretsResponse,
    operation_id="list_secrets_full",
)
def list_secrets(
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> ListSecretsResponse:
    relationships = StorageRelationshipService(services.deployment_resources).for_workspace(
        workspace_id
    )
    return ListSecretsResponse.from_records(
        services.secrets.list(workspace=workspace_id),
        workloads=relationships.secrets,
    )


@router.get(
    "/api/v1/secrets/{name}",
    response_model=GetSecretResponse,
    operation_id="get_secret",
)
def get_secret(
    name: str,
    workspace_id: read_workspace,
    services: ApiServices = Depends(current_services),
) -> GetSecretResponse:
    record = services.secrets.get(name, workspace=workspace_id)
    relationships = StorageRelationshipService(services.deployment_resources).for_workspace(
        workspace_id
    )
    return GetSecretResponse(
        secret=SecretWireRecord.from_record(
            record,
            workloads=relationships.secrets.get(record.name, ()),
        )
    )


@router.post(
    "/api/v1/secrets",
    response_model=CreateSecretResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_secret",
)
def create_secret(
    request: SecretPayload,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> CreateSecretResponse:
    return CreateSecretResponse.from_record(
        services.secrets.create(request.name, request.value, workspace=workspace_id)
    )


@router.post(
    "/api/v1/secrets/{name}",
    response_model=SecretMaskedSetResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="set_secret",
)
def set_secret(
    name: str,
    request: SecretValuePayload,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> SecretMaskedSetResponse:
    record = services.secrets.set(name, request.value, workspace=workspace_id)
    return SecretMaskedSetResponse(name=record.name, value=record.masked())


@router.patch(
    "/api/v1/secrets/{name}",
    response_model=UpdateSecretResponse,
    operation_id="update_secret",
)
def update_secret(
    name: str,
    request: SecretValuePayload,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> UpdateSecretResponse:
    services.secrets.update(name, request.value, workspace=workspace_id)
    return UpdateSecretResponse()


@router.delete(
    "/api/v1/secrets/{name}",
    response_model=SecretDeleteResponse,
    operation_id="delete_secret",
)
def delete_secret(
    name: str,
    workspace_id: write_workspace,
    services: ApiServices = Depends(current_services),
) -> SecretDeleteResponse:
    services.secrets.delete(name, workspace=workspace_id)
    return SecretDeleteResponse()

from __future__ import annotations

from compute.aws_connections import (
    AwsAccountConnectionAuthorization,
    AwsAccountConnectionDirectory,
    AwsAccountConnectionService,
)
from fastapi import APIRouter, Depends, status
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.http.aws_connections import (
    AwsAuthorizationGenerationResponse,
    AwsComputeConfigurationUpdateRequest,
    AwsConnectionAuthorization,
    AwsConnectionAuthorizationResponse,
    AwsConnectionCreateRequest,
    AwsConnectionCurrentResponse,
    AwsConnectionCustomerAction,
    AwsConnectionReconnectRequest,
    AwsConnectionResponse,
    AwsManagedAuthorizationResponse,
)

from api.server.auth import admin_access, read_user, write_user
from api.server.service_dependencies import (
    aws_account_connection_directory,
    aws_account_connection_service,
)

router = APIRouter(prefix="/api/v1/aws-connection", tags=["compute"])

_CONNECTION_DETAIL: dict[AwsAccountConnectionPhase, str] = {
    AwsAccountConnectionPhase.AwaitingAuthorization: "Complete authorization in AWS.",
    AwsAccountConnectionPhase.Validating: "Checking AWS authorization.",
    AwsAccountConnectionPhase.Ready: "AWS compute is available for your workspaces.",
    AwsAccountConnectionPhase.Degraded: (
        "AWS authorization needs attention before new workloads can be placed."
    ),
    AwsAccountConnectionPhase.ReconnectPending: (
        "AWS compute remains available while replacement authorization is completed."
    ),
    AwsAccountConnectionPhase.RetiringAuthorization: (
        "AWS compute remains available while previous authorization is removed."
    ),
    AwsAccountConnectionPhase.DisconnectDraining: "Removing AWS compute.",
    AwsAccountConnectionPhase.Revoking: "Revoking AWS authorization.",
    AwsAccountConnectionPhase.VerifyingRevocation: "Verifying AWS removal.",
    AwsAccountConnectionPhase.ActionRequired: (
        "Automatic AWS cleanup needs attention before removal can finish."
    ),
}


_ERROR_MESSAGE_LIMIT = 512


def _error_message(message: str) -> str | None:
    """Project the stored AWS failure text, truncated to the published contract bound."""
    if not message:
        return None
    return message[:_ERROR_MESSAGE_LIMIT]


def _generation_response(
    generation: AwsAccountAuthorizationGeneration | None,
) -> AwsAuthorizationGenerationResponse | None:
    if generation is None:
        return None
    managed = generation.managed_authorization
    return AwsAuthorizationGenerationResponse(
        generation=generation.generation,
        authorization_mode=generation.authorization_mode,
        managed_authorization=(
            AwsManagedAuthorizationResponse(
                stack_name=managed.stack_name,
                region=managed.region,
                generation=managed.generation,
                stack_id=managed.stack_id,
                template_version=managed.template_version,
                template_sha256=managed.template_sha256,
            )
            if managed is not None
            else None
        ),
        phase=generation.phase,
        last_validation_started_at=generation.last_validation_started_at,
        last_validated_at=generation.last_validated_at,
        error_code=generation.error_code,
        error_message=_error_message(generation.error_message),
        created_at=generation.created_at,
        updated_at=generation.updated_at,
    )


def _response(connection: AwsAccountConnection) -> AwsConnectionResponse:
    return AwsConnectionResponse(
        id=connection.id,
        account_id=connection.account_id,
        phase=connection.phase,
        revision=connection.revision,
        compute=connection.compute,
        hosts_workloads=connection.hosts_workloads,
        can_manage_existing_capacity=connection.can_manage_existing_capacity,
        available_actions=connection.available_actions,
        detail=_CONNECTION_DETAIL[connection.phase],
        customer_action=(
            AwsConnectionCustomerAction(
                url=connection.customer_action_url,
                label=connection.customer_action_label,
            )
            if connection.customer_action_label
            else None
        ),
        next_retry_at=connection.next_reconcile_at,
        active_authorization=_generation_response(connection.active_authorization),
        pending_authorization=_generation_response(connection.pending_authorization),
        retiring_authorization=_generation_response(connection.retiring_authorization),
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _authorization_response(
    result: AwsAccountConnectionAuthorization,
) -> AwsConnectionAuthorizationResponse:
    return AwsConnectionAuthorizationResponse(
        connection=_response(result.connection),
        authorization=AwsConnectionAuthorization(
            url=result.authorization_url,
            external_id=result.external_id,
        ),
    )


@router.get(
    "",
    response_model=AwsConnectionCurrentResponse,
    operation_id="get_current_aws_account_connection",
)
def get_current_aws_account_connection(
    user_id: read_user,
    directory: AwsAccountConnectionDirectory = Depends(aws_account_connection_directory),
) -> AwsConnectionCurrentResponse:
    connection = directory.current(user_id=user_id)
    return AwsConnectionCurrentResponse(
        connection=_response(connection) if connection is not None else None
    )


@router.post(
    "",
    response_model=AwsConnectionAuthorizationResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="connect_aws_account",
)
def connect_aws_account(
    request: AwsConnectionCreateRequest,
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionAuthorizationResponse:
    return _authorization_response(service.connect(request, user_id=user_id))


@router.post(
    "/fleet",
    response_model=AwsConnectionAuthorizationResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="connect_aws_fleet_account",
)
def connect_aws_fleet_account(
    request: AwsConnectionCreateRequest,
    _auth: admin_access,
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionAuthorizationResponse:
    """Connect the platform's own account as shared capacity.

    Separate from the customer route rather than a field on it. Declaring an
    account to be the fleet makes its machines serve every customer and bill to
    the platform, so it is an administrator's act and cannot be reached by a
    credential that only speaks for one account.
    """
    return _authorization_response(service.connect(request, user_id=user_id, platform_fleet=True))


@router.put(
    "/compute",
    response_model=AwsConnectionResponse,
    operation_id="update_aws_account_compute_configuration",
)
def update_aws_account_compute_configuration(
    request: AwsComputeConfigurationUpdateRequest,
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionResponse:
    """Set how capacity is provisioned in this account, for every workspace it backs."""
    return _response(
        service.update_compute_configuration(
            user_id=user_id,
            expected_revision=request.expected_revision,
            configuration=request.compute,
        )
    )


@router.post(
    "/validate",
    response_model=AwsConnectionResponse,
    operation_id="validate_aws_account_connection",
)
def validate_aws_account_connection(
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionResponse:
    return _response(service.validate(user_id=user_id))


@router.post(
    "/reconnect",
    response_model=AwsConnectionAuthorizationResponse,
    operation_id="reconnect_aws_account",
)
def reconnect_aws_account(
    request: AwsConnectionReconnectRequest,
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionAuthorizationResponse:
    return _authorization_response(service.reconnect(request, user_id=user_id))


@router.delete(
    "",
    response_model=AwsConnectionCurrentResponse,
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="remove_aws_account_connection",
)
def remove_aws_account_connection(
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionCurrentResponse:
    connection = service.remove(user_id=user_id)
    return AwsConnectionCurrentResponse(
        connection=_response(connection) if connection is not None else None
    )


@router.delete(
    "/reconnect",
    response_model=AwsConnectionResponse,
    operation_id="cancel_aws_account_reconnect",
)
def cancel_aws_account_reconnect(
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionResponse:
    return _response(service.cancel_reconnect(user_id=user_id))


@router.post(
    "/retry",
    response_model=AwsConnectionResponse,
    operation_id="retry_aws_account_connection",
)
def retry_aws_account_connection(
    user_id: write_user,
    service: AwsAccountConnectionService = Depends(aws_account_connection_service),
) -> AwsConnectionResponse:
    return _response(service.retry(user_id=user_id))


__all__ = ["router"]

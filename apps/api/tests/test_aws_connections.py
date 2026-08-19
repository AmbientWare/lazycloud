from __future__ import annotations

from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.aws_connections import (
    AwsAccountConnectionService,
    AwsAccountConnectionValidationError,
    AwsAccountPoolDrain,
    AwsAccountRevocationAction,
    AwsAuthorizationCleanupResult,
)
from fastapi.testclient import TestClient
from identity.auth import TokenIssuer
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPlan,
    AwsAccountConnection,
    AwsAccountConnectionErrorCode,
    AwsAccountNetwork,
    AwsAccountValidationResult,
    AwsAuthorizationCleanupStatus,
    AwsManagedAuthorizationReference,
)
from shared.http.aws_connections import (
    AwsConnectionCurrentResponse,
    AwsConnectionResponse,
)
from tests.service_fixtures import workspace_owner_user_id

ACCOUNT_ID = "123456789012"
VALIDATED_AT = datetime(2026, 7, 15, 12, tzinfo=UTC)
TEMPLATE_SHA256 = "a" * 64


@dataclass(slots=True)
class _AuthorizationPlanner:
    generations: list[int] = field(default_factory=list)

    def plan(
        self,
        *,
        user_id: str,
        connection_id: str,
        generation: int,
        account_id: str,
        external_id: str,
        role_arn: str | None,
        active_authorization: AwsAccountAuthorizationGeneration | None,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        network: AwsAccountNetwork | None = None,
    ) -> AwsAccountAuthorizationPlan:
        del user_id, connection_id, external_id, active_authorization
        self.generations.append(generation)
        managed = role_arn is None
        suffix = account_id[-4:]
        node_role = node_role_arn or f"arn:aws:iam::{account_id}:role/compute-node-{suffix}"
        node_profile = node_instance_profile_arn or (
            f"arn:aws:iam::{account_id}:instance-profile/compute-node-{suffix}"
        )
        return AwsAccountAuthorizationPlan(
            role_arn=(
                role_arn
                or f"arn:aws:iam::{account_id}:role/compute-connection-{suffix}-g{generation}"
            ),
            authorization_mode=(
                AwsAccountAuthorizationMode.ManagedStack
                if managed
                else AwsAccountAuthorizationMode.ExistingRole
            ),
            managed_authorization=(
                AwsManagedAuthorizationReference(
                    stack_name=f"compute-connection-{suffix}-g{generation}",
                    region="us-east-1",
                    generation=generation,
                    template_version="test.v1",
                    template_sha256=TEMPLATE_SHA256,
                )
                if managed
                else None
            ),
            authorization_url=(
                f"https://console.aws.amazon.com/cloudformation/g{generation}" if managed else None
            ),
            node_role_arn=node_role,
            node_instance_profile_arn=node_profile,
        )


@dataclass(slots=True)
class _ConnectionValidator:
    failures: list[AwsAccountConnectionErrorCode | None] = field(default_factory=list)
    failure_message: str | None = None

    def validate(
        self,
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountValidationResult:
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise AwsAccountConnectionValidationError(
                self.failure_message or f"validation failed: {failure.value}",
                code=failure,
            )
        assert connection.node_role_arn is not None
        assert connection.node_instance_profile_arn is not None
        managed = authorization.managed_authorization
        return AwsAccountValidationResult(
            account_id=connection.account_id,
            role_arn=authorization.role_arn,
            managed_authorization=(
                managed.model_copy(
                    update={
                        "stack_id": (
                            f"arn:aws:cloudformation:us-east-1:{connection.account_id}:"
                            f"stack/{managed.stack_name}/stack-id"
                        )
                    }
                )
                if managed is not None
                else None
            ),
            node_role_arn=connection.node_role_arn,
            node_instance_profile_arn=connection.node_instance_profile_arn,
            validated_at=VALIDATED_AT,
        )


@dataclass(slots=True)
class _AuthorizationLifecycle:
    results: list[AwsAuthorizationCleanupStatus] = field(
        default_factory=lambda: [AwsAuthorizationCleanupStatus.Complete]
    )
    operation_ids: list[str] = field(default_factory=list)
    action_url: str | None = "https://console.aws.amazon.com/cloudformation/final"
    action_label: str = "Review cleanup in AWS"

    def reconcile_authorization_cleanup(
        self,
        *,
        account_id: str,
        external_id: str,
        authorization: AwsAccountAuthorizationGeneration,
        operation_id: str,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
        remove_node_identity: bool,
    ) -> AwsAuthorizationCleanupResult:
        del (
            account_id,
            external_id,
            authorization,
            node_role_arn,
            node_instance_profile_arn,
            remove_node_identity,
        )
        self.operation_ids.append(operation_id)
        outcome = self.results.pop(0) if self.results else AwsAuthorizationCleanupStatus.Complete
        if outcome is AwsAuthorizationCleanupStatus.ActionRequired:
            return AwsAuthorizationCleanupResult(
                status=outcome,
                error_code=AwsAccountConnectionErrorCode.StackDrift,
                error_message="AWS could not delete the authorization stack.",
                customer_action=AwsAccountRevocationAction(
                    url=self.action_url,
                    label=self.action_label,
                ),
            )
        return AwsAuthorizationCleanupResult(status=outcome)


@dataclass(slots=True)
class _PoolDrainer:
    remaining: list[int] = field(default_factory=lambda: [1, 0])

    def request_connection_drain(
        self,
        connection_id: str,
        *,
        workspace_ids: Sequence[str],
    ) -> AwsAccountPoolDrain:
        del connection_id, workspace_ids
        remaining = self.remaining.pop(0) if self.remaining else 0
        return AwsAccountPoolDrain(total_pools=1, remaining_pools=remaining)


def _client(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
    *,
    validator: _ConnectionValidator | None = None,
    authorization_lifecycle: _AuthorizationLifecycle | None = None,
    pool_drainer: _PoolDrainer | None = None,
) -> tuple[TestClient, AwsAccountConnectionService]:
    aws_connections = AwsAccountConnectionService(
        context=isolated_services.context,
        authorization_planner=_AuthorizationPlanner(),
        validator=validator or _ConnectionValidator(),
        authorization_lifecycle=authorization_lifecycle or _AuthorizationLifecycle(),
        pool_drainer=pool_drainer or _PoolDrainer(),
        workspace_changes=isolated_services.workspace_changes,
        provider_poll_seconds=0,
    )
    services = replace(isolated_services, aws_connections=aws_connections)
    token = _account_token(isolated_services, "aws-connection")
    client_stack = ExitStack()
    request.addfinalizer(client_stack.close)
    client = client_stack.enter_context(
        TestClient(
            create_app(services),
            headers={"Authorization": f"Bearer {token}"},
        )
    )
    return client, aws_connections


def _account_token(services: ApiServices, name: str) -> str:
    """A credential that names a person: connecting an account is an account-level act."""
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
    user_id = workspace_owner_user_id(services.context, workspace_id)
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, _ = issuer.issue_for_user(session, name, user_id=user_id)
    issuer.committed()
    return raw_token


def test_connection_status_is_available_when_aws_mutations_are_disabled(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    token = _account_token(isolated_services, "aws-status")
    client_stack = ExitStack()
    request.addfinalizer(client_stack.close)
    client = client_stack.enter_context(
        TestClient(
            create_app(isolated_services),
            headers={"Authorization": f"Bearer {token}"},
        )
    )

    status_response = client.get("/api/v1/aws-connection")
    connect_response = client.post(
        "/api/v1/aws-connection",
        json={"account_id": ACCOUNT_ID},
    )

    assert status_response.status_code == 200
    assert status_response.json() == {"connection": None}
    assert connect_response.status_code == 503
    assert connect_response.json() == {
        "detail": "AWS account connections are not enabled",
        "code": "upstream_unavailable",
    }


def test_unfinished_setup_can_be_canceled_without_active_authorization(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    client, _aws_connections = _client(isolated_services, request)
    created = client.post("/api/v1/aws-connection", json={"account_id": ACCOUNT_ID})
    assert created.status_code == 201

    removed = client.delete("/api/v1/aws-connection")

    assert removed.status_code == 202
    assert removed.json() == {"connection": None}
    assert client.get("/api/v1/aws-connection").json() == {"connection": None}


def test_managed_connection_projects_its_nonsecret_stack_identity(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    client, _aws_connections = _client(isolated_services, request)
    created = client.post("/api/v1/aws-connection", json={"account_id": ACCOUNT_ID})
    assert created.status_code == 201

    validated = client.post("/api/v1/aws-connection/validate")

    assert validated.status_code == 200
    managed = validated.json()["active_authorization"]["managed_authorization"]
    assert managed == {
        "stack_name": f"compute-connection-{ACCOUNT_ID[-4:]}-g1",
        "region": "us-east-1",
        "generation": 1,
        "stack_id": (
            f"arn:aws:cloudformation:us-east-1:{ACCOUNT_ID}:"
            f"stack/compute-connection-{ACCOUNT_ID[-4:]}-g1/stack-id"
        ),
        "template_version": "test.v1",
        "template_sha256": TEMPLATE_SHA256,
    }


def test_terminal_cleanup_failure_is_recoverable_from_the_row(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    lifecycle = _AuthorizationLifecycle(results=[AwsAuthorizationCleanupStatus.ActionRequired])
    client, aws_connections = _client(
        isolated_services,
        request,
        authorization_lifecycle=lifecycle,
        pool_drainer=_PoolDrainer(remaining=[0]),
    )
    client.post("/api/v1/aws-connection", json={"account_id": ACCOUNT_ID})
    client.post("/api/v1/aws-connection/validate")
    client.delete("/api/v1/aws-connection")

    aws_connections.reconcile_due()
    aws_connections.reconcile_due()
    current = client.get("/api/v1/aws-connection")
    connection = AwsConnectionCurrentResponse.model_validate_json(current.content).connection
    assert connection is not None

    assert connection.phase == "action_required"
    assert [action.value for action in connection.available_actions] == ["retry"]
    assert connection.customer_action is not None
    assert connection.customer_action.label == "Review cleanup in AWS"
    retried = client.post("/api/v1/aws-connection/retry")
    assert retried.status_code == 200
    assert retried.json()["phase"] == "revoking"


def test_label_only_existing_role_recovery_survives_the_http_boundary(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    lifecycle = _AuthorizationLifecycle(
        results=[AwsAuthorizationCleanupStatus.ActionRequired],
        action_url=None,
        action_label="Revoke the authorization role in AWS",
    )
    client, aws_connections = _client(
        isolated_services,
        request,
        authorization_lifecycle=lifecycle,
        pool_drainer=_PoolDrainer(remaining=[0]),
    )
    role_arn = f"arn:aws:iam::{ACCOUNT_ID}:role/customer-compute-control-v1"
    client.post(
        "/api/v1/aws-connection",
        json={"account_id": ACCOUNT_ID, "role_arn": role_arn},
    )
    client.post("/api/v1/aws-connection/validate")
    client.delete("/api/v1/aws-connection")

    aws_connections.reconcile_due()
    aws_connections.reconcile_due()
    current = client.get("/api/v1/aws-connection")
    connection = AwsConnectionCurrentResponse.model_validate_json(current.content).connection
    assert connection is not None

    assert connection.phase == "action_required"
    assert connection.customer_action is not None
    assert connection.customer_action.url is None
    assert connection.customer_action.label == "Revoke the authorization role in AWS"


def test_validation_failure_distinguishes_active_health_from_pending_reconnect(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    validator = _ConnectionValidator(
        failures=[
            None,
            AwsAccountConnectionErrorCode.PermissionDrift,
            AwsAccountConnectionErrorCode.AssumeRoleDenied,
        ]
    )
    client, _aws_connections = _client(
        isolated_services,
        request,
        validator=validator,
    )
    existing_role = f"arn:aws:iam::{ACCOUNT_ID}:role/customer-compute-control-v1"
    created = client.post(
        "/api/v1/aws-connection",
        json={"account_id": ACCOUNT_ID, "role_arn": existing_role},
    )
    assert created.json()["authorization"]["url"] is None
    assert created.json()["authorization"]["external_id"]
    initial_validation = client.post("/api/v1/aws-connection/validate")
    assert initial_validation.status_code == 200, initial_validation.text
    assert initial_validation.json()["phase"] == "ready"

    degraded_response = client.post("/api/v1/aws-connection/validate")
    assert degraded_response.status_code == 200, degraded_response.text
    degraded = AwsConnectionResponse.model_validate_json(degraded_response.content)
    assert degraded.phase == "degraded"
    assert degraded.active_authorization is not None
    assert degraded.active_authorization.error_code == "permission_drift"
    reconnect = client.post(
        "/api/v1/aws-connection/reconnect",
        json={"role_arn": existing_role},
    )
    assert reconnect.status_code == 200
    pending_response = client.post("/api/v1/aws-connection/validate")
    pending_failure = AwsConnectionResponse.model_validate_json(pending_response.content)
    assert pending_failure.phase == "reconnect_pending"
    assert pending_failure.active_authorization is not None
    assert pending_failure.active_authorization.generation == 1
    assert pending_failure.pending_authorization is not None
    assert pending_failure.pending_authorization.error_code == "assume_role_denied"


def test_degraded_authorization_projects_its_truncated_provider_diagnostic(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    diagnostic = (
        f"User: arn:aws:sts::{ACCOUNT_ID}:assumed-role/customer-compute-control-v1/session "
        "is not authorized to perform: ec2:ModifyImageAttribute. "
        "Encoded authorization failure message: " + "A" * 900
    )
    validator = _ConnectionValidator(
        failures=[None, AwsAccountConnectionErrorCode.PermissionDrift],
        failure_message=diagnostic,
    )
    client, _aws_connections = _client(isolated_services, request, validator=validator)
    existing_role = f"arn:aws:iam::{ACCOUNT_ID}:role/customer-compute-control-v1"
    client.post(
        "/api/v1/aws-connection",
        json={"account_id": ACCOUNT_ID, "role_arn": existing_role},
    )
    assert client.post("/api/v1/aws-connection/validate").json()["phase"] == "ready"

    degraded_response = client.post("/api/v1/aws-connection/validate")

    assert degraded_response.status_code == 200, degraded_response.text
    degraded = AwsConnectionResponse.model_validate_json(degraded_response.content)
    assert degraded.active_authorization is not None
    error_message = degraded.active_authorization.error_message
    assert error_message is not None
    assert "not authorized to perform: ec2:ModifyImageAttribute" in error_message
    assert error_message == diagnostic[:512]


def test_initial_validation_failure_stays_retryable_without_an_active_generation(
    isolated_services: ApiServices,
    request: pytest.FixtureRequest,
) -> None:
    validator = _ConnectionValidator(
        failures=[AwsAccountConnectionErrorCode.AssumeRoleDenied, None]
    )
    client, _aws_connections = _client(
        isolated_services,
        request,
        validator=validator,
    )
    created = client.post("/api/v1/aws-connection", json={"account_id": ACCOUNT_ID})
    assert created.status_code == 201

    failed = client.post("/api/v1/aws-connection/validate")
    assert failed.status_code == 200, failed.text
    assert failed.json()["phase"] == "awaiting_authorization"
    assert failed.json()["available_actions"] == ["authorize", "validate", "remove"]
    assert failed.json()["active_authorization"] is None
    assert failed.json()["pending_authorization"]["error_code"] == "assume_role_denied"

    recovered = client.post("/api/v1/aws-connection/validate")
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["phase"] == "ready"
    assert recovered.json()["active_authorization"]["generation"] == 1
    assert recovered.json()["pending_authorization"] is None

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from agent.binary import AgentBinarySettings
from compute.aws_connections import AwsAccountConnectionValidationError
from networking.settings import (
    BackendRouteSettings,
    TailnetControlSettings,
    TailnetRuntimeSettings,
)
from networking.tailnet import TailnetRuntimeMode
from provider_aws import (
    AwsAccountAuthorizationCleanupResult,
    AwsAccountAuthorizationCleanupStatus,
    AwsAccountAuthorizationValidation,
    AwsAccountAuthorizationValidationInput,
    AwsActiveAccountAuthorization,
    AwsExistingAccountAuthorization,
    AwsExistingAccountAuthorizationValidation,
    AwsExistingAccountAuthorizationValidationInput,
    AwsManagedNodeIdentity,
    AwsPendingAccountAuthorization,
    aws_account_connection_template_identity,
)
from provider_clients import (
    AwsAccountConnectionComponents,
    configured_aws_account_connection_components,
    configured_aws_compute_catalog,
)
from provider_clients.aws_connections import (
    _AwsAccountAuthorizationLifecycle,
    _AwsAccountConnectionValidator,
    _require_current_managed_template,
)
from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings
from pydantic import SecretStr
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionErrorCode,
    AwsAccountConnectionPhase,
    AwsAuthorizationCleanupStatus,
    AwsManagedAuthorizationReference,
)

_ACCOUNT_ID = "123456789012"
_EXTERNAL_ID = "connection-external-id-0123456789abcdef"
_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/compute-connection-abc-g1"
_NODE_ROLE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:role/compute-node-abc"
_NODE_PROFILE_ARN = f"arn:aws:iam::{_ACCOUNT_ID}:instance-profile/compute-node-abc"
_OPERATION_ID = "cleanup-12345678123441238123123456789abc"


@dataclass(frozen=True, slots=True)
class _AwsOwnerSettings:
    connection: AwsAccountConnectionSettings
    capacity: AwsCapacitySettings
    artifact: AgentBinarySettings
    runtime: TailnetRuntimeSettings
    control: TailnetControlSettings
    backend_route: BackendRouteSettings
    gateway_origin: str = "https://control.example.com"


def _enabled_settings() -> _AwsOwnerSettings:
    template_identity = aws_account_connection_template_identity()
    return _AwsOwnerSettings(
        connection=AwsAccountConnectionSettings(
            enabled=True,
            template_url=(
                "https://assets.s3.us-east-1.amazonaws.com/templates/"
                f"{template_identity.sha256}/connection.json"
            ),
            control_principal_arn=("arn:aws:iam::210987654321:role/customer-cloud-control"),
        ),
        capacity=AwsCapacitySettings(
            worker_image_digest=(f"registry.example.com/worker@sha256:{'c' * 64}"),
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'b' * 64}/"
                "lazycloud-agent-linux-amd64"
            ),
            instance_hourly_micros={"i4i.xlarge": 340_000},
            cpu_ami_ids={"us-east-1": "ami-0123456789abcdef0"},
            gpu_ami_ids={"us-east-1": "ami-0fedcba9876543210"},
        ),
        artifact=AgentBinarySettings(
            binary_dir=Path("/tmp/agent-binarys"),
            binary_version="0.1.0",
            binary_sha256_by_arch={"amd64": "b" * 64},
        ),
        runtime=TailnetRuntimeSettings(
            mode=TailnetRuntimeMode.Managed,
            hostname="lazycloud-control-plane",
        ),
        control=TailnetControlSettings(
            oauth_client_id="oauth-client-id",
            oauth_client_secret=SecretStr("oauth-client-secret"),
        ),
        backend_route=BackendRouteSettings(auth_key=SecretStr("0123456789abcdef0123456789abcdef")),
    )


def _connection_components(
    settings: _AwsOwnerSettings,
) -> AwsAccountConnectionComponents | None:
    return configured_aws_account_connection_components(
        settings.connection,
        capacity=settings.capacity,
        gateway_origin=settings.gateway_origin,
        internal_origin="http://lazycloud-control-plane.tailnet-example.ts.net:9000",
        tailnet_runtime=settings.runtime,
        tailnet_control=settings.control,
        backend_route=settings.backend_route,
    )


def test_aws_connection_composition_rejects_non_content_addressed_template_url() -> None:
    settings = _enabled_settings()
    settings = replace(
        settings,
        connection=settings.connection.model_copy(
            update={
                "template_url": ("https://assets.s3.us-east-1.amazonaws.com/templates/latest.json")
            }
        ),
    )

    with pytest.raises(ValueError, match="does not identify the bundled template digest"):
        _connection_components(settings)


def test_aws_compute_catalog_only_includes_launchable_priced_region_types() -> None:
    settings = _enabled_settings()
    capacity = settings.capacity.model_copy(
        update={
            "cpu_ami_ids": {
                "us-west-2": "ami-1234567890abcdef0",
                "us-east-1": "ami-0123456789abcdef0",
            },
            "gpu_ami_ids": {
                "us-west-2": "ami-2345678901abcdef0",
            },
            "instance_hourly_micros": {
                "g6.xlarge": 804_000,
                "i4i.xlarge": 340_000,
            },
        }
    )

    catalog = configured_aws_compute_catalog(
        capacity,
        settings.artifact,
    )

    assert [region.region for region in catalog] == ["us-east-1", "us-west-2"]
    assert [instance.instance_type for instance in catalog[0].instances] == ["i4i.xlarge"]
    assert [instance.instance_type for instance in catalog[1].instances] == [
        "i4i.xlarge",
        "g6.xlarge",
    ]


class _CleanupControl:
    def __init__(self, result: AwsAccountAuthorizationCleanupResult) -> None:
        self.result = result
        self.authorization: (
            AwsPendingAccountAuthorization | AwsExistingAccountAuthorization | None
        ) = None
        self.external_id = ""
        self.operation_id = ""
        self.remove_node_identity = False

    def reconcile_authorization_cleanup(
        self,
        authorization: AwsPendingAccountAuthorization | AwsExistingAccountAuthorization,
        *,
        external_id: SecretStr,
        operation_id: str,
        remove_node_identity: bool,
    ) -> AwsAccountAuthorizationCleanupResult:
        self.authorization = authorization
        self.external_id = external_id.get_secret_value()
        self.operation_id = operation_id
        self.remove_node_identity = remove_node_identity
        return self.result


def _managed_generation() -> AwsAccountAuthorizationGeneration:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    stack_name = "compute-connection-abc-g1"
    return AwsAccountAuthorizationGeneration(
        id="12345678-1234-4123-8123-123456789abc",
        generation=1,
        role_arn=_ROLE_ARN,
        authorization_mode=AwsAccountAuthorizationMode.ManagedStack,
        managed_authorization=AwsManagedAuthorizationReference(
            stack_name=stack_name,
            region="us-east-1",
            generation=1,
            stack_id=(
                f"arn:aws:cloudformation:us-east-1:{_ACCOUNT_ID}:stack/{stack_name}/stack-id"
            ),
            template_version="2026-07-16.v9",
            template_sha256="a" * 64,
        ),
        phase=AwsAccountAuthorizationPhase.Ready,
        validation_generation=1,
        last_validated_at=now,
        created_at=now,
        updated_at=now,
    )


class _StubAuthorizationValidator:
    def __init__(self, validation: AwsAccountAuthorizationValidation) -> None:
        self.validation = validation

    def validate_authorization(
        self,
        validation_input: AwsAccountAuthorizationValidationInput,
    ) -> AwsAccountAuthorizationValidation:
        del validation_input
        return self.validation

    def validate_existing_authorization(
        self,
        validation_input: AwsExistingAccountAuthorizationValidationInput,
    ) -> AwsExistingAccountAuthorizationValidation:
        del validation_input
        raise AssertionError("existing-role validation is not part of this scenario")


class _RecordingImageSharing:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def grant_launch_permission(
        self,
        *,
        region: str,
        account_id: str,
        ami_ids: Sequence[str],
    ) -> tuple[str, ...]:
        granted = tuple(sorted(ami_ids))
        self.calls.append((region, account_id, granted))
        return granted


def _current_managed_generation() -> AwsAccountAuthorizationGeneration:
    generation = _managed_generation()
    reference = generation.managed_authorization
    assert reference is not None
    identity = aws_account_connection_template_identity()
    return generation.model_copy(
        update={
            "phase": AwsAccountAuthorizationPhase.Validating,
            "managed_authorization": reference.model_copy(
                update={
                    "template_version": identity.version,
                    "template_sha256": identity.sha256,
                }
            ),
        }
    )


def _validating_connection(
    generation: AwsAccountAuthorizationGeneration,
) -> AwsAccountConnection:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    return AwsAccountConnection(
        id="12345678-1234-4123-8123-123456789abd",
        user_id="12345678-1234-4123-8123-123456789abe",
        account_id=_ACCOUNT_ID,
        external_id=_EXTERNAL_ID,
        phase=AwsAccountConnectionPhase.Validating,
        pending_authorization=generation,
        node_role_arn=_NODE_ROLE_ARN,
        node_instance_profile_arn=_NODE_PROFILE_ARN,
        created_at=now,
        updated_at=now,
    )


def _managed_validation() -> AwsAccountAuthorizationValidation:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    stack_name = "compute-connection-abc-g1"
    node_identity = AwsManagedNodeIdentity(
        role_name="compute-node-abc",
        role_arn=_NODE_ROLE_ARN,
        instance_profile_name="compute-node-abc",
        instance_profile_arn=_NODE_PROFILE_ARN,
    )
    return AwsAccountAuthorizationValidation(
        authorization=AwsActiveAccountAuthorization(
            account_id=_ACCOUNT_ID,
            region="us-east-1",
            generation=1,
            stack_name=stack_name,
            role_name=stack_name,
            role_arn=_ROLE_ARN,
            external_id_sha256=hashlib.sha256(_EXTERNAL_ID.encode()).hexdigest(),
            node_identity=node_identity,
            stack_id=(
                f"arn:aws:cloudformation:us-east-1:{_ACCOUNT_ID}:stack/{stack_name}/stack-id"
            ),
            validated_at=now,
        ),
        caller_arn=_ROLE_ARN,
        node_identity=node_identity,
        vpc_id="vpc-0123456789abcdef0",
        subnet_ids=("subnet-a", "subnet-b"),
        security_group_id="sg-0123456789abcdef0",
    )


def test_managed_validation_shares_only_regional_capacity_amis() -> None:
    generation = _current_managed_generation()
    sharing = _RecordingImageSharing()
    validator = _AwsAccountConnectionValidator(
        _StubAuthorizationValidator(_managed_validation()),
        image_sharing=sharing,
        capacity_ami_ids={
            "us-east-1": ("ami-0123456789abcdef0",),
            "us-west-2": ("ami-0fedcba9876543210",),
        },
    )

    result = validator.validate(_validating_connection(generation), generation)

    assert sharing.calls == [("us-east-1", _ACCOUNT_ID, ("ami-0123456789abcdef0",))]
    assert result.managed_authorization is not None
    assert result.managed_authorization.shared_ami_ids == ("ami-0123456789abcdef0",)

    unconfigured_sharing = _RecordingImageSharing()
    unconfigured = _AwsAccountConnectionValidator(
        _StubAuthorizationValidator(_managed_validation()),
        image_sharing=unconfigured_sharing,
        capacity_ami_ids={},
    )

    unconfigured_result = unconfigured.validate(_validating_connection(generation), generation)

    assert unconfigured_sharing.calls == []
    assert unconfigured_result.managed_authorization is not None
    assert unconfigured_result.managed_authorization.shared_ami_ids == ()


def test_validation_rejects_obsolete_managed_connection_template() -> None:
    with pytest.raises(AwsAccountConnectionValidationError) as exc_info:
        _require_current_managed_template(_managed_generation())

    assert exc_info.value.code is AwsAccountConnectionErrorCode.StackDrift


def test_cleanup_adapter_preserves_operation_and_exact_node_identity() -> None:
    control = _CleanupControl(
        AwsAccountAuthorizationCleanupResult(
            status=AwsAccountAuthorizationCleanupStatus.Pending,
            role_assumable=True,
            stack_exists=True,
            stack_status="DELETE_IN_PROGRESS",
        )
    )
    lifecycle = _AwsAccountAuthorizationLifecycle(control)

    result = lifecycle.reconcile_authorization_cleanup(
        account_id=_ACCOUNT_ID,
        external_id=_EXTERNAL_ID,
        authorization=_managed_generation(),
        operation_id=_OPERATION_ID,
        node_role_arn=_NODE_ROLE_ARN,
        node_instance_profile_arn=_NODE_PROFILE_ARN,
        remove_node_identity=True,
    )

    assert result.status is AwsAuthorizationCleanupStatus.Pending
    assert control.operation_id == _OPERATION_ID
    assert control.external_id == _EXTERNAL_ID
    assert control.remove_node_identity is True
    assert isinstance(control.authorization, AwsPendingAccountAuthorization)
    assert control.authorization.node_identity.role_arn == _NODE_ROLE_ARN
    assert control.authorization.node_identity.instance_profile_arn == _NODE_PROFILE_ARN


def test_cleanup_adapter_maps_managed_stack_failure_to_customer_action() -> None:
    console_url = (
        "https://console.aws.amazon.com/cloudformation/home?region=us-east-1"
        "#/stacks/stackinfo?stackId=stack-id"
    )
    lifecycle = _AwsAccountAuthorizationLifecycle(
        _CleanupControl(
            AwsAccountAuthorizationCleanupResult(
                status=AwsAccountAuthorizationCleanupStatus.ActionRequired,
                role_assumable=True,
                stack_exists=True,
                stack_status="DELETE_FAILED",
                error_message="AWS could not delete the managed authorization stack.",
                console_url=console_url,
            )
        )
    )

    result = lifecycle.reconcile_authorization_cleanup(
        account_id=_ACCOUNT_ID,
        external_id=_EXTERNAL_ID,
        authorization=_managed_generation(),
        operation_id=_OPERATION_ID,
        node_role_arn=_NODE_ROLE_ARN,
        node_instance_profile_arn=_NODE_PROFILE_ARN,
        remove_node_identity=True,
    )

    assert result.status is AwsAuthorizationCleanupStatus.ActionRequired
    assert result.error_code is AwsAccountConnectionErrorCode.StackDrift
    assert result.customer_action is not None
    assert result.customer_action.url == console_url

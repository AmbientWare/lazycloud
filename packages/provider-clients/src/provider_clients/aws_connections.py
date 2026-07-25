from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlparse

from compute.aws_connections import (
    AwsAccountAuthorizationLifecycle,
    AwsAccountAuthorizationPlanner,
    AwsAccountConnectionValidationError,
    AwsAccountConnectionValidator,
    AwsAccountRevocationAction,
    AwsAuthorizationCleanupResult,
)
from compute.bucket_access import (
    AwsNodeBucketAccessController,
    ConnectedBucketAccessGrant,
)
from networking.settings import (
    BackendRouteSettings,
    ProviderNetworkClass,
    TailnetControlSettings,
    TailnetRuntimeSettings,
    validate_provider_network_configuration,
)
from provider_aws import (
    AwsAccountAuthorizationCleanupResult,
    AwsAccountAuthorizationValidation,
    AwsAccountAuthorizationValidationError,
    AwsAccountAuthorizationValidationErrorCode,
    AwsAccountAuthorizationValidationInput,
    AwsAccountConnectionPlanner,
    AwsAccountConnectionTarget,
    AwsAccountConnectionTemplatePublication,
    AwsActiveAccountAuthorization,
    AwsExistingAccountAuthorization,
    AwsExistingAccountAuthorizationValidation,
    AwsExistingAccountAuthorizationValidationInput,
    AwsManagedNodeIdentity,
    AwsNodeBucketAccessGrant,
    AwsPendingAccountAuthorization,
    AwsProviderControlError,
    AwsProviderControlErrorCode,
    Boto3AwsAccountAuthorizationControl,
    Boto3AwsAccountConnectionValidator,
    Boto3AwsCapacityImageSharing,
    Boto3AwsNodeBucketAccessControl,
    aws_account_connection_template_identity,
)
from pydantic import SecretStr
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPlan,
    AwsAccountConnection,
    AwsAccountConnectionErrorCode,
    AwsAccountValidationResult,
    AwsAuthorizationCleanupStatus,
    AwsManagedAuthorizationReference,
)
from shared.timestamps import utc_now

from provider_clients.settings import AwsAccountConnectionSettings, AwsCapacitySettings


@dataclass(frozen=True, slots=True)
class AwsAccountConnectionComponents:
    authorization_planner: AwsAccountAuthorizationPlanner
    validator: AwsAccountConnectionValidator
    authorization_lifecycle: AwsAccountAuthorizationLifecycle
    bucket_access: AwsNodeBucketAccessController


@dataclass(frozen=True, slots=True)
class _AwsNodeBucketAccessController:
    control: Boto3AwsNodeBucketAccessControl
    region: str = "us-east-1"

    def reconcile(
        self,
        connection: AwsAccountConnection,
        grants: tuple[ConnectedBucketAccessGrant, ...],
    ) -> None:
        node_role_arn = connection.node_role_arn
        node_instance_profile_arn = connection.node_instance_profile_arn
        if node_role_arn is None or node_instance_profile_arn is None:
            raise ValueError("AWS connection is missing its managed node identity")
        managed = connection.managed_authorization
        self.control.reconcile(
            target=AwsAccountConnectionTarget(
                account_id=connection.account_id,
                region=managed.region if managed is not None else self.region,
                role_arn=connection.role_arn,
                external_id=SecretStr(connection.external_id),
                node_role_arn=node_role_arn,
                node_instance_profile_arn=node_instance_profile_arn,
            ),
            grants=tuple(
                AwsNodeBucketAccessGrant(
                    bucket=grant.bucket,
                    prefix=grant.prefix,
                    read_only=grant.read_only,
                )
                for grant in grants
            ),
        )


@dataclass(frozen=True, slots=True)
class _AwsAccountAuthorizationPlanner:
    planner: AwsAccountConnectionPlanner

    def plan(
        self,
        *,
        workspace_id: str,
        connection_id: str,
        generation: int,
        account_id: str,
        external_id: str,
        role_arn: str | None,
        active_authorization: AwsAccountAuthorizationGeneration | None,
        node_role_arn: str | None,
        node_instance_profile_arn: str | None,
    ) -> AwsAccountAuthorizationPlan:
        secret_external_id = SecretStr(external_id)
        if role_arn is not None:
            existing = self.planner.plan_existing_role(
                workspace_id=workspace_id,
                connection_id=connection_id,
                account_id=account_id,
                role_arn=role_arn,
                external_id=secret_external_id,
            )
            if generation != (active_authorization.generation + 1 if active_authorization else 1):
                raise ValueError("existing-role authorization generation is not sequential")
            return AwsAccountAuthorizationPlan(
                role_arn=existing.role_arn,
                authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
                node_role_arn=existing.node_identity.role_arn,
                node_instance_profile_arn=existing.node_identity.instance_profile_arn,
            )
        plan = (
            self.planner.plan_initial(
                workspace_id=workspace_id,
                connection_id=connection_id,
                account_id=account_id,
                external_id=secret_external_id,
            )
            if active_authorization is None
            else self.planner.plan_replacement(
                workspace_id=workspace_id,
                connection_id=connection_id,
                account_id=account_id,
                external_id=secret_external_id,
                active=_managed_active_authorization(
                    account_id=account_id,
                    external_id=external_id,
                    authorization=active_authorization,
                    node_role_arn=node_role_arn,
                    node_instance_profile_arn=node_instance_profile_arn,
                ),
            )
        )
        if plan.pending.generation != generation:
            raise ValueError("managed authorization generation is not sequential")
        return AwsAccountAuthorizationPlan(
            role_arn=plan.pending.role_arn,
            authorization_url=plan.authorization_url,
            authorization_mode=AwsAccountAuthorizationMode.ManagedStack,
            managed_authorization=AwsManagedAuthorizationReference(
                stack_name=plan.pending.stack_name,
                region=plan.pending.region,
                generation=plan.pending.generation,
                template_version=plan.template_version,
                template_sha256=plan.template_sha256,
            ),
            node_role_arn=plan.pending.node_identity.role_arn,
            node_instance_profile_arn=plan.pending.node_identity.instance_profile_arn,
        )


class _AwsAccountAuthorizationValidator(Protocol):
    def validate_authorization(
        self,
        validation_input: AwsAccountAuthorizationValidationInput,
    ) -> AwsAccountAuthorizationValidation: ...

    def validate_existing_authorization(
        self,
        validation_input: AwsExistingAccountAuthorizationValidationInput,
    ) -> AwsExistingAccountAuthorizationValidation: ...


class _AwsCapacityImageSharing(Protocol):
    def grant_launch_permission(
        self,
        *,
        region: str,
        account_id: str,
        ami_ids: Sequence[str],
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class _AwsAccountConnectionValidator:
    validator: _AwsAccountAuthorizationValidator
    image_sharing: _AwsCapacityImageSharing
    capacity_ami_ids: Mapping[str, tuple[str, ...]]
    region: str = "us-east-1"

    def validate(
        self,
        connection: AwsAccountConnection,
        authorization: AwsAccountAuthorizationGeneration,
    ) -> AwsAccountValidationResult:
        if connection.node_role_arn is None or connection.node_instance_profile_arn is None:
            raise AwsAccountConnectionValidationError(
                "AWS authorization has not returned the shared node IAM resources"
            )
        try:
            if authorization.authorization_mode is AwsAccountAuthorizationMode.ManagedStack:
                _require_current_managed_template(authorization)
                managed = self.validator.validate_authorization(
                    AwsAccountAuthorizationValidationInput(
                        pending=_managed_pending_authorization(
                            account_id=connection.account_id,
                            external_id=connection.external_id,
                            authorization=authorization,
                            node_role_arn=connection.node_role_arn,
                            node_instance_profile_arn=connection.node_instance_profile_arn,
                        ),
                        external_id=SecretStr(connection.external_id),
                    )
                )
                managed_reference = authorization.managed_authorization
                if managed_reference is None:
                    raise ValueError("managed authorization has no durable stack reference")
                shared_ami_ids = self._share_capacity_images(
                    region=managed.authorization.region,
                    account_id=managed.authorization.account_id,
                )
                return AwsAccountValidationResult(
                    account_id=managed.authorization.account_id,
                    role_arn=managed.authorization.role_arn,
                    managed_authorization=managed_reference.model_copy(
                        update={
                            "stack_id": managed.authorization.stack_id,
                            "vpc_id": managed.vpc_id,
                            "subnet_ids": managed.subnet_ids,
                            "security_group_id": managed.security_group_id,
                            "shared_ami_ids": shared_ami_ids,
                        }
                    ),
                    node_role_arn=managed.node_identity.role_arn,
                    node_instance_profile_arn=managed.node_identity.instance_profile_arn,
                    validated_at=managed.authorization.validated_at,
                )
            existing = self.validator.validate_existing_authorization(
                AwsExistingAccountAuthorizationValidationInput(
                    authorization=_existing_authorization(
                        account_id=connection.account_id,
                        external_id=connection.external_id,
                        authorization=authorization,
                        node_role_arn=connection.node_role_arn,
                        node_instance_profile_arn=connection.node_instance_profile_arn,
                        region=self.region,
                    ),
                    external_id=SecretStr(connection.external_id),
                )
            )
        except AwsAccountAuthorizationValidationError as exc:
            raise AwsAccountConnectionValidationError(
                exc.message,
                code=(
                    AwsAccountConnectionErrorCode.ExternalIdNotEnforced
                    if exc.code is AwsAccountAuthorizationValidationErrorCode.ExternalIdNotEnforced
                    else AwsAccountConnectionErrorCode.PermissionDrift
                ),
            ) from exc
        except AwsProviderControlError as exc:
            raise AwsAccountConnectionValidationError(
                exc.detail,
                code=_connection_error_code(exc),
            ) from exc
        return AwsAccountValidationResult(
            account_id=existing.authorization.account_id,
            role_arn=existing.authorization.role_arn,
            node_role_arn=existing.node_identity.role_arn,
            node_instance_profile_arn=existing.node_identity.instance_profile_arn,
            validated_at=utc_now(),
        )

    def _share_capacity_images(self, *, region: str, account_id: str) -> tuple[str, ...]:
        ami_ids = self.capacity_ami_ids.get(region, ())
        if not ami_ids:
            return ()
        return self.image_sharing.grant_launch_permission(
            region=region,
            account_id=account_id,
            ami_ids=ami_ids,
        )


def _require_current_managed_template(
    authorization: AwsAccountAuthorizationGeneration,
) -> None:
    reference = authorization.managed_authorization
    identity = aws_account_connection_template_identity()
    if reference is None or (
        reference.template_version != identity.version
        or reference.template_sha256 != identity.sha256
    ):
        raise AwsAccountConnectionValidationError(
            "AWS managed authorization uses an obsolete connection template; reconnect it",
            code=AwsAccountConnectionErrorCode.StackDrift,
        )


@dataclass(frozen=True, slots=True)
class _AwsAccountAuthorizationLifecycle:
    control: _AwsAccountAuthorizationControl
    region: str = "us-east-1"

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
        native_authorization = (
            _managed_authorization(
                account_id=account_id,
                external_id=external_id,
                authorization=authorization,
                node_role_arn=node_role_arn,
                node_instance_profile_arn=node_instance_profile_arn,
            )
            if authorization.authorization_mode is AwsAccountAuthorizationMode.ManagedStack
            else _existing_authorization(
                account_id=account_id,
                external_id=external_id,
                authorization=authorization,
                node_role_arn=node_role_arn,
                node_instance_profile_arn=node_instance_profile_arn,
                region=self.region,
            )
        )
        result = _authorization_control_call(
            lambda: self.control.reconcile_authorization_cleanup(
                native_authorization,
                external_id=SecretStr(external_id),
                operation_id=operation_id,
                remove_node_identity=remove_node_identity,
            )
        )
        status = AwsAuthorizationCleanupStatus(result.status.value)
        if status is not AwsAuthorizationCleanupStatus.ActionRequired:
            return AwsAuthorizationCleanupResult(status=status)
        existing_role = authorization.authorization_mode is AwsAccountAuthorizationMode.ExistingRole
        return AwsAuthorizationCleanupResult(
            status=status,
            error_code=(
                AwsAccountConnectionErrorCode.PermissionDrift
                if existing_role
                else AwsAccountConnectionErrorCode.StackDrift
            ),
            error_message=result.error_message,
            customer_action=AwsAccountRevocationAction(
                url=result.console_url,
                label=(
                    "Revoke or delete authorization role" if existing_role else "Open AWS cleanup"
                ),
            ),
        )


class _AwsAccountAuthorizationControl(Protocol):
    def reconcile_authorization_cleanup(
        self,
        authorization: AwsPendingAccountAuthorization | AwsExistingAccountAuthorization,
        *,
        external_id: SecretStr,
        operation_id: str,
        remove_node_identity: bool,
    ) -> AwsAccountAuthorizationCleanupResult: ...


def _managed_node_identity(
    node_role_arn: str | None,
    node_instance_profile_arn: str | None,
) -> AwsManagedNodeIdentity:
    if node_role_arn is None or node_instance_profile_arn is None:
        raise ValueError("AWS connection is missing its managed node identity")
    return AwsManagedNodeIdentity(
        role_name=node_role_arn.rsplit("/", maxsplit=1)[-1],
        role_arn=node_role_arn,
        instance_profile_name=node_instance_profile_arn.rsplit("/", maxsplit=1)[-1],
        instance_profile_arn=node_instance_profile_arn,
    )


def _managed_pending_authorization(
    *,
    account_id: str,
    external_id: str,
    authorization: AwsAccountAuthorizationGeneration,
    node_role_arn: str | None,
    node_instance_profile_arn: str | None,
) -> AwsPendingAccountAuthorization:
    reference = authorization.managed_authorization
    if reference is None:
        raise ValueError("managed authorization has no durable stack reference")
    return AwsPendingAccountAuthorization(
        account_id=account_id,
        region=reference.region,
        generation=authorization.generation,
        stack_name=reference.stack_name,
        role_name=authorization.role_arn.rsplit("/", maxsplit=1)[-1],
        role_arn=authorization.role_arn,
        external_id_sha256=hashlib.sha256(external_id.encode()).hexdigest(),
        node_identity=_managed_node_identity(node_role_arn, node_instance_profile_arn),
    )


def _managed_authorization(
    *,
    account_id: str,
    external_id: str,
    authorization: AwsAccountAuthorizationGeneration,
    node_role_arn: str | None,
    node_instance_profile_arn: str | None,
) -> AwsPendingAccountAuthorization:
    reference = authorization.managed_authorization
    if reference is None:
        raise ValueError("managed authorization has no durable stack reference")
    if reference.stack_id is None:
        return _managed_pending_authorization(
            account_id=account_id,
            external_id=external_id,
            authorization=authorization,
            node_role_arn=node_role_arn,
            node_instance_profile_arn=node_instance_profile_arn,
        )
    return _managed_active_authorization(
        account_id=account_id,
        external_id=external_id,
        authorization=authorization,
        node_role_arn=node_role_arn,
        node_instance_profile_arn=node_instance_profile_arn,
    )


def _managed_active_authorization(
    *,
    account_id: str,
    external_id: str,
    authorization: AwsAccountAuthorizationGeneration,
    node_role_arn: str | None,
    node_instance_profile_arn: str | None,
) -> AwsActiveAccountAuthorization:
    pending = _managed_pending_authorization(
        account_id=account_id,
        external_id=external_id,
        authorization=authorization,
        node_role_arn=node_role_arn,
        node_instance_profile_arn=node_instance_profile_arn,
    )
    reference = authorization.managed_authorization
    if reference is None or reference.stack_id is None or authorization.last_validated_at is None:
        raise ValueError("managed authorization has not completed validation")
    return AwsActiveAccountAuthorization(
        account_id=pending.account_id,
        region=pending.region,
        generation=pending.generation,
        stack_name=pending.stack_name,
        role_name=pending.role_name,
        role_arn=pending.role_arn,
        external_id_sha256=pending.external_id_sha256,
        node_identity=pending.node_identity,
        stack_id=reference.stack_id,
        validated_at=authorization.last_validated_at,
    )


def _existing_authorization(
    *,
    account_id: str,
    external_id: str,
    authorization: AwsAccountAuthorizationGeneration,
    node_role_arn: str | None,
    node_instance_profile_arn: str | None,
    region: str,
) -> AwsExistingAccountAuthorization:
    return AwsExistingAccountAuthorization(
        account_id=account_id,
        region=region,
        role_arn=authorization.role_arn,
        external_id_sha256=hashlib.sha256(external_id.encode()).hexdigest(),
        node_identity=_managed_node_identity(node_role_arn, node_instance_profile_arn),
    )


def _capacity_ami_ids_by_region(capacity: AwsCapacitySettings) -> dict[str, tuple[str, ...]]:
    regions = capacity.cpu_ami_ids.keys() | capacity.gpu_ami_ids.keys()
    return {
        region: tuple(
            sorted(
                {
                    ami_id
                    for catalog in (capacity.cpu_ami_ids, capacity.gpu_ami_ids)
                    if (ami_id := catalog.get(region)) is not None
                }
            )
        )
        for region in sorted(regions)
    }


def _connection_error_code(
    error: AwsProviderControlError,
) -> AwsAccountConnectionErrorCode:
    detail = error.detail.casefold()
    if "another account" in detail or "outside the account" in detail:
        return AwsAccountConnectionErrorCode.AccountMismatch
    if "stack" in detail:
        return AwsAccountConnectionErrorCode.StackDrift
    if error.code is AwsProviderControlErrorCode.ControlRoleUnavailable:
        return AwsAccountConnectionErrorCode.AssumeRoleDenied
    if error.code is AwsProviderControlErrorCode.UpstreamUnavailable:
        return AwsAccountConnectionErrorCode.UpstreamUnavailable
    return AwsAccountConnectionErrorCode.PermissionDrift


def _authorization_control_call[ResultT](operation: Callable[[], ResultT]) -> ResultT:
    try:
        return operation()
    except AwsProviderControlError as exc:
        raise AwsAccountConnectionValidationError(
            exc.detail,
            code=_connection_error_code(exc),
        ) from exc


def configured_aws_account_connection_components(
    settings: AwsAccountConnectionSettings,
    *,
    capacity: AwsCapacitySettings,
    gateway_origin: str,
    tailnet_runtime: TailnetRuntimeSettings,
    tailnet_control: TailnetControlSettings,
    backend_route: BackendRouteSettings,
) -> AwsAccountConnectionComponents | None:
    if not settings.enabled:
        return None
    validate_provider_network_configuration(
        ProviderNetworkClass.Remote,
        gateway_origin=gateway_origin,
        runtime=tailnet_runtime,
        control=tailnet_control,
        backend_route=backend_route,
    )
    template_identity = aws_account_connection_template_identity()
    template_url = settings.template_url
    template_path = unquote(urlparse(template_url).path)
    if template_identity.sha256 not in template_path:
        raise ValueError(
            "configured AWS connection template URL does not identify the bundled template digest"
        )
    planner = AwsAccountConnectionPlanner(
        platform_principal_arn=settings.control_principal_arn,
        publication=AwsAccountConnectionTemplatePublication(
            url=template_url,
            sha256=template_identity.sha256,
        ),
    )
    return AwsAccountConnectionComponents(
        authorization_planner=_AwsAccountAuthorizationPlanner(planner),
        validator=_AwsAccountConnectionValidator(
            Boto3AwsAccountConnectionValidator.from_default_chain(),
            image_sharing=Boto3AwsCapacityImageSharing.from_default_chain(),
            capacity_ami_ids=_capacity_ami_ids_by_region(capacity),
        ),
        authorization_lifecycle=_AwsAccountAuthorizationLifecycle(
            Boto3AwsAccountAuthorizationControl.from_default_chain()
        ),
        bucket_access=_AwsNodeBucketAccessController(
            Boto3AwsNodeBucketAccessControl.from_default_chain()
        ),
    )


__all__ = [
    "AwsAccountConnectionComponents",
    "configured_aws_account_connection_components",
]

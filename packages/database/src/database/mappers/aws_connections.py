from __future__ import annotations

from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountConnection,
    AwsAccountNetwork,
    AwsAuthorizationCleanupTombstone,
    AwsManagedAuthorizationReference,
)
from shared.timestamps import to_utc, to_utc_or_none

from database.tables.aws_connections import (
    AuthorizationColumns,
    AwsAccountConnectionTable,
    AwsAccountNetworkTable,
    AwsAuthorizationCleanupTombstoneTable,
    AwsAuthorizationGenerationTable,
)


def authorization_from_row(row: AuthorizationColumns) -> AwsAccountAuthorizationGeneration:
    return AwsAccountAuthorizationGeneration.model_validate(
        {
            "id": row.authorization_id,
            "generation": row.authorization_generation,
            "role_arn": row.authorization_role_arn,
            "authorization_mode": row.authorization_mode,
            "phase": row.authorization_phase,
            "validation_generation": row.authorization_validation_generation,
            "last_validation_started_at": to_utc_or_none(
                row.authorization_last_validation_started_at
            ),
            "last_validated_at": to_utc_or_none(row.authorization_last_validated_at),
            "expires_at": to_utc_or_none(row.authorization_expires_at),
            "error_code": row.authorization_error_code,
            "error_message": row.authorization_error_message,
            "created_at": to_utc(row.authorization_created_at),
            "updated_at": to_utc(row.authorization_updated_at),
            "authorization_stack": row.authorization_stack,
            "managed_authorization": AwsManagedAuthorizationReference.model_validate(
                {
                    "generation": row.authorization_generation,
                    "stack_name": row.managed_stack_name,
                    "region": row.managed_region,
                    "stack_id": row.managed_stack_id,
                    "template_version": row.managed_template_version,
                    "template_sha256": row.managed_template_sha256,
                    "shared_ami_ids": row.managed_shared_ami_ids,
                }
            )
            if row.managed_stack_name is not None
            else None,
        }
    )


def write_authorization(
    row: AuthorizationColumns, record: AwsAccountAuthorizationGeneration
) -> None:
    row.authorization_id = record.id
    row.authorization_generation = record.generation
    row.authorization_role_arn = record.role_arn
    row.authorization_mode = record.authorization_mode.value
    row.authorization_phase = record.phase.value
    row.authorization_validation_generation = record.validation_generation
    row.authorization_last_validation_started_at = record.last_validation_started_at
    row.authorization_last_validated_at = record.last_validated_at
    row.authorization_expires_at = record.expires_at
    row.authorization_error_code = (
        record.error_code.value if record.error_code is not None else None
    )
    row.authorization_error_message = record.error_message
    row.authorization_created_at = record.created_at
    row.authorization_updated_at = record.updated_at
    row.authorization_stack = (
        record.authorization_stack.model_dump(mode="json")
        if record.authorization_stack is not None
        else None
    )
    managed = record.managed_authorization
    row.managed_stack_name = managed.stack_name if managed is not None else None
    row.managed_region = managed.region if managed is not None else None
    row.managed_stack_id = managed.stack_id if managed is not None else None
    row.managed_template_version = managed.template_version if managed is not None else None
    row.managed_template_sha256 = managed.template_sha256 if managed is not None else None
    row.managed_shared_ami_ids = list(managed.shared_ami_ids) if managed is not None else None


def connection_from_row(row: AwsAccountConnectionTable) -> AwsAccountConnection:
    generations = {item.slot: authorization_from_row(item) for item in row.authorizations}
    return AwsAccountConnection.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "account_id": row.account_id,
            "external_id": row.external_id,
            "platform_fleet": row.platform_fleet,
            "pool": row.pool,
            "phase": row.phase,
            "active_authorization": generations.get("active"),
            "pending_authorization": generations.get("pending"),
            "retiring_authorization": generations.get("retiring"),
            "node_role_arn": row.node_role_arn,
            "node_instance_profile_arn": row.node_instance_profile_arn,
            "networks": {
                item.region: AwsAccountNetwork(
                    vpc_id=item.vpc_id,
                    subnet_ids=tuple(item.subnet_ids),
                    security_group_id=item.security_group_id,
                )
                for item in row.network_rows
            },
            "drain_total_pools": row.drain_total_pools,
            "drain_remaining_pools": row.drain_remaining_pools,
            "customer_action_url": row.customer_action_url,
            "customer_action_label": row.customer_action_label,
            "revision": row.revision,
            "next_reconcile_at": to_utc_or_none(row.next_reconcile_at),
            "claim_token": row.claim_token,
            "claim_expires_at": to_utc_or_none(row.claim_expires_at),
            "reconcile_attempt_count": row.reconcile_attempt_count,
            "bucket_access_reconcile_pending": row.bucket_access_reconcile_pending,
            "provider_operation_id": row.provider_operation_id,
            "provider_operation_started_at": to_utc_or_none(row.provider_operation_started_at),
            "last_error": row.last_error,
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


def write_connection(row: AwsAccountConnectionTable, record: AwsAccountConnection) -> None:
    row.user_id = record.user_id
    row.account_id = record.account_id
    row.external_id = record.external_id
    row.platform_fleet = record.platform_fleet
    row.pool = record.pool
    row.phase = record.phase.value
    row.node_role_arn = record.node_role_arn
    row.node_instance_profile_arn = record.node_instance_profile_arn
    row.drain_total_pools = record.drain_total_pools
    row.drain_remaining_pools = record.drain_remaining_pools
    row.customer_action_url = record.customer_action_url
    row.customer_action_label = record.customer_action_label
    row.revision = record.revision
    row.next_reconcile_at = record.next_reconcile_at
    row.claim_token = record.claim_token
    row.claim_expires_at = record.claim_expires_at
    row.reconcile_attempt_count = record.reconcile_attempt_count
    row.bucket_access_reconcile_pending = record.bucket_access_reconcile_pending
    row.provider_operation_id = record.provider_operation_id
    row.provider_operation_started_at = record.provider_operation_started_at
    row.last_error = record.last_error
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    existing_generations = {item.slot: item for item in row.authorizations}
    generations: list[AwsAuthorizationGenerationTable] = []
    for slot, authorization in (
        ("active", record.active_authorization),
        ("pending", record.pending_authorization),
        ("retiring", record.retiring_authorization),
    ):
        if authorization is None:
            continue
        generation = existing_generations.get(slot)
        if generation is None:
            generation = AwsAuthorizationGenerationTable(connection_id=record.id, slot=slot)
        write_authorization(generation, authorization)
        generations.append(generation)
    row.authorizations = generations
    existing_networks = {item.region: item for item in row.network_rows}
    networks: list[AwsAccountNetworkTable] = []
    for region, network in record.networks.items():
        item = existing_networks.get(region)
        if item is None:
            item = AwsAccountNetworkTable(connection_id=record.id, region=region)
        item.vpc_id = network.vpc_id
        item.subnet_ids = list(network.subnet_ids)
        item.security_group_id = network.security_group_id
        networks.append(item)
    row.network_rows = networks


def tombstone_from_row(
    row: AwsAuthorizationCleanupTombstoneTable,
) -> AwsAuthorizationCleanupTombstone:
    return AwsAuthorizationCleanupTombstone.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "connection_id": row.connection_id,
            "account_id": row.account_id,
            "external_id": row.external_id,
            "authorization": authorization_from_row(row),
            "node_role_arn": row.node_role_arn,
            "node_instance_profile_arn": row.node_instance_profile_arn,
            "remove_node_identity": row.remove_node_identity,
            "status": row.status,
            "provider_operation_id": row.provider_operation_id,
            "revision": row.revision,
            "next_reconcile_at": to_utc(row.next_reconcile_at),
            "expires_at": to_utc(row.expires_at),
            "claim_token": row.claim_token,
            "claim_expires_at": to_utc_or_none(row.claim_expires_at),
            "reconcile_attempt_count": row.reconcile_attempt_count,
            "last_error": row.last_error,
            "created_at": to_utc(row.created_at),
            "updated_at": to_utc(row.updated_at),
        }
    )


def write_tombstone(
    row: AwsAuthorizationCleanupTombstoneTable, record: AwsAuthorizationCleanupTombstone
) -> None:
    row.user_id = record.user_id
    row.connection_id = record.connection_id
    row.account_id = record.account_id
    row.external_id = record.external_id
    row.node_role_arn = record.node_role_arn
    row.node_instance_profile_arn = record.node_instance_profile_arn
    row.remove_node_identity = record.remove_node_identity
    row.status = record.status.value
    row.provider_operation_id = record.provider_operation_id
    row.revision = record.revision
    row.next_reconcile_at = record.next_reconcile_at
    row.expires_at = record.expires_at
    row.claim_token = record.claim_token
    row.claim_expires_at = record.claim_expires_at
    row.reconcile_attempt_count = record.reconcile_attempt_count
    row.last_error = record.last_error
    row.created_at = record.created_at
    row.updated_at = record.updated_at
    write_authorization(row, record.authorization)

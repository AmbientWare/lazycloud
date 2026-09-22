from uuid import uuid4

import pytest
from alembic import command
from database.migrations import alembic_config
from database.repositories.compute import ComputeUnitRepository
from database.repositories.identity import TokenRepository, UserRepository, WorkspaceRepository
from identity.platform import PlatformNamespaceService
from provider_aws.platform import AwsPlatformBinding
from pydantic import SecretStr
from shared.aws_connections import AwsAccountNetwork
from shared.identity import TokenKind, TokenStatus, WorkspaceKind
from shared.placement import Placement
from sqlalchemy import text
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database


def test_platform_adoption_fences_identity_and_preserves_tenant_and_capacity_history(
    postgres_database_url: URL,
) -> None:
    dsn = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(dsn), "0004_image_build_attempts")
    database = DatabaseClient.from_settings(
        DatabaseSettings(url=dsn, application_name=DatabaseApplicationName.Test)
    )
    workspace_id, connection_id, unit_id, machine_id, worker_id = (str(uuid4()) for _ in range(5))
    binding = AwsPlatformBinding(
        provider_ref=f"aws:{connection_id}",
        account_id="123456789012",
        region="us-east-1",
        role_arn="arn:aws:iam::123456789012:role/control",
        node_role_arn="arn:aws:iam::123456789012:role/node",
        node_instance_profile_arn="arn:aws:iam::123456789012:instance-profile/node",
        external_id=SecretStr("migration-test-external-identifier"),
        networks={
            "us-east-1": AwsAccountNetwork(
                vpc_id="vpc-01234567",
                subnet_ids=("subnet-01234567", "subnet-89abcdef"),
                security_group_id="sg-01234567",
            )
        },
    )
    try:
        with database.session() as session:
            user = UserRepository(session).create(display_name="tenant")
            session.execute(
                text("""
                INSERT INTO workspaces (id,name,status,signing_key,storage_backend,
                    storage_prefix,labels,metadata)
                VALUES (:id,'default','active','retained-signing-key','local','','{}','{}')
            """),
                {"id": workspace_id},
            )
            session.execute(
                text("""
                INSERT INTO aws_account_connections (id,user_id,account_id,external_id,
                    pool,phase,revision,
                    reconcile_attempt_count,platform_fleet,node_role_arn,node_instance_profile_arn,
                    drain_total_pools,drain_remaining_pools,customer_action_label,bucket_access_reconcile_pending,last_error)
                VALUES (:id,:user_id,:account,:external,'lazycloud','ready',1,0,true,
                    :role,:profile,0,0,'',false,'')
            """),
                {
                    "id": connection_id,
                    "user_id": user.id,
                    "account": binding.account_id,
                    "external": binding.external_id.get_secret_value(),
                    "role": binding.node_role_arn,
                    "profile": binding.node_instance_profile_arn,
                },
            )
            session.execute(
                text("""
                INSERT INTO aws_authorization_generations (connection_id,slot,authorization_id,
                    authorization_generation,authorization_role_arn,authorization_mode,authorization_phase,
                    authorization_validation_generation,authorization_last_validated_at,authorization_error_message,
                    authorization_created_at,authorization_updated_at)
                VALUES (:id,'active',gen_random_uuid(),1,:role,'existing_role','ready',
                    0,now(),'',now(),now())
            """),
                {"id": connection_id, "role": binding.role_arn},
            )
            network = binding.networks["us-east-1"]
            session.execute(
                text("""
                INSERT INTO aws_account_networks (connection_id,region,vpc_id,
                    subnet_ids,security_group_id)
                VALUES (:id,'us-east-1',:vpc,:subnets,:sg)
            """),
                {
                    "id": connection_id,
                    "vpc": network.vpc_id,
                    "subnets": list(network.subnet_ids),
                    "sg": network.security_group_id,
                },
            )
            session.execute(
                text("""
                INSERT INTO compute_units (
                    id, workspace_id, capacity_owner_id, capacity_owner_kind,
                    capacity_owner_source, name, pool, provider, selector, status, source,
                    provider_ref, provider_connection_id, capacity_mode, visibility,
                    region, offer_id, capability_key, generation, desired_machines,
                    initial_machines, min_machines, max_machines, observed_machines,
                    phase, provider_attributes, scaling_enabled, default_eligible, priority,
                    min_free_cpu_millicores, min_free_memory_mib, min_free_gpu_count,
                    worker_cpu_millicores, worker_memory_mib, worker_gpu_type, worker_gpu_count,
                    worker_runtimes, worker_preemptible, idle_drain_timeout_seconds,
                    scale_up_cooldown_seconds, scale_down_cooldown_seconds,
                    registration_timeout_seconds, root_volume_gib, fallback,
                    provider_resource_id, launch_attempt_baseline, platform_fleet,
                    offer_storage_mib, offer_availability_zone, supplier_cpu_unit,
                    supplier_cpu_count, replacement_machine_id, replacement_template_version)
                VALUES (
                    :id, :workspace, :id, 'pooled_provider', 'provider', 'existing-fleet',
                    'lazycloud', 'aws', '', 'ready', 'workspace_policy', :provider, :connection,
                    'pooled', 'internal', 'us-east-1', 'kept-offer', 'kept-capability',
                    9, 1, 0, 0, 3, 0, 'ready', '{}', false, false, 0,
                    0, 0, 0, 0, 0, '', 0, ARRAY['runsc'], false, 300, 0, 0, 300, 200,
                    'internal', 'existing-autoscaling-group', 0, true, 0, '', 'vcpu', 0, '', '')
                """),
                {
                    "id": unit_id,
                    "workspace": workspace_id,
                    "provider": binding.provider_ref,
                    "connection": connection_id,
                },
            )
            session.execute(
                text(
                    "INSERT INTO machines (id, capacity_owner_id, workspace_id, pool, provider, "
                    "status, gpu_count, labels) VALUES (:id, :owner, :workspace, 'default', "
                    "'local', 'stopped', 0, '{}'::jsonb)"
                ),
                {"id": machine_id, "owner": unit_id, "workspace": workspace_id},
            )
            session.execute(
                text("""
                INSERT INTO workers (
                    id, machine_id, workspace_id, pool, status, labels, last_seen_at)
                VALUES (:id, :machine, :workspace, 'default', 'stopped', '{}', now())
                """),
                {"id": worker_id, "machine": machine_id, "workspace": workspace_id},
            )
            token = TokenRepository(session).create(
                name="old-worker",
                token_hash="unusable-test-hash",
                prefix="test",
                kind=TokenKind.Worker,
                workspace_id=workspace_id,
                worker_id=worker_id,
            )
            human = TokenRepository(session).create(
                name="retained-human",
                token_hash="unusable-human-test-hash",
                prefix="test",
                kind=TokenKind.User,
                user_id=user.id,
            )
            credential_id = str(uuid4())
            session.execute(
                text("""
                INSERT INTO compute_join_credentials (
                    id, token_hash, user_id, workspace_id, capacity_owner_id,
                    pool, machine_id, status, max_uses, use_count, expires_at)
                VALUES (:id, :hash, :user, :workspace, :owner,
                    'lazycloud', '', 'active', 1, 0, now() + interval '30 minutes')
                """),
                {
                    "id": credential_id,
                    "hash": "0" * 64,
                    "user": user.id,
                    "workspace": workspace_id,
                    "owner": unit_id,
                },
            )
        with pytest.raises(RuntimeError, match="does not match"):
            bootstrap_database(dsn, platform_bindings={binding.provider_ref: "wrong-binding"})
        with database.session() as session:
            assert (
                session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0004_image_build_attempts"
            )
            assert session.scalar(text("SELECT count(*) FROM aws_account_connections")) == 1
        bootstrap_database(
            dsn, platform_bindings={binding.provider_ref: binding.migration_fingerprint()}
        )
        namespace = PlatformNamespaceService(database).initialize()
        with database.session() as session:
            tenant = WorkspaceRepository(session).get(workspace_id)
            assert tenant is not None and tenant.kind is WorkspaceKind.Tenant
            assert tenant.signing_key == "retained-signing-key"
            adopted = ComputeUnitRepository(session).get(unit_id)
            assert adopted is not None
            assert adopted.workspace_id == namespace.id
            assert adopted.provider_connection_id is None
            assert adopted.placement == Placement.platform()
            assert adopted.platform_fleet and adopted.generation == 9
            assert adopted.provider_state.resource_id == "existing-autoscaling-group"
            assert adopted.provider_state.attributes["namespace_id"] == workspace_id
            old_worker = TokenRepository(session).get_across_workspaces(token.id)
            retained_human = TokenRepository(session).get_across_workspaces(human.id)
            assert old_worker is not None and old_worker.status is TokenStatus.Revoked
            assert retained_human is not None and retained_human.status is TokenStatus.Active
            row = session.execute(
                text(
                    "SELECT workspace_id::text,user_id,status "
                    "FROM compute_join_credentials WHERE id=:id"
                ),
                {"id": credential_id},
            ).one()
            assert tuple(row) == (namespace.id, None, "revoked")
            assert session.scalar(text("SELECT count(*) FROM aws_account_connections")) == 0
    finally:
        database.dispose()

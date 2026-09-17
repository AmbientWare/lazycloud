from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from database.migrations import alembic_config
from database.repositories.compute import ComputeUnitRepository
from database.repositories.identity import TokenRepository, UserRepository, WorkspaceRepository
from database.tables.compute import ComputeJoinCredentialTable, ComputeUnitTable
from database.tables.orchestration import MachineTable, WorkerTable
from identity.platform import PlatformNamespaceService
from provider_aws.platform import AwsPlatformBinding
from pydantic import SecretStr
from shared.aws_connections import AwsAccountNetwork
from shared.identity import TokenKind, TokenStatus, WorkspaceKind
from shared.timestamps import utc_now
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
            session.add(
                ComputeUnitTable(
                    id=unit_id,
                    workspace_id=workspace_id,
                    name="existing-fleet",
                    pool="lazycloud",
                    capacity_owner_id=unit_id,
                    capacity_owner_kind="pooled_provider",
                    capacity_owner_source="provider",
                    provider="aws",
                    provider_ref=binding.provider_ref,
                    provider_connection_id=connection_id,
                    platform_fleet=True,
                    capacity_mode="pooled",
                    visibility="internal",
                    region="us-east-1",
                    offer_id="kept-offer",
                    capability_key="kept-capability",
                    generation=9,
                    desired_machines=1,
                    max_machines=3,
                    worker_runtimes=["runsc"],
                    provider_resource_id="existing-autoscaling-group",
                    launch_attempt_baseline=0,
                    offer_storage_mib=0,
                    offer_availability_zone="",
                    supplier_cpu_unit="vcpu",
                    supplier_cpu_count=0,
                    replacement_machine_id="",
                    replacement_template_version="",
                )
            )
            session.add(
                MachineTable(
                    id=machine_id,
                    capacity_owner_id=unit_id,
                    workspace_id=workspace_id,
                    status="stopped",
                    labels={},
                )
            )
            session.flush()
            session.add(
                WorkerTable(
                    id=worker_id,
                    machine_id=machine_id,
                    workspace_id=workspace_id,
                    status="stopped",
                    labels={},
                    last_seen_at=utc_now(),
                )
            )
            session.flush()
            unit = ComputeUnitRepository(session).get(unit_id)
            assert unit is not None
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
            credential = ComputeJoinCredentialTable(
                token_hash="0" * 64,
                user_id=user.id,
                workspace_id=workspace_id,
                capacity_owner_id=unit_id,
                pool=unit.pool,
                created_by_token_id=None,
                max_uses=1,
                expires_at=utc_now() + timedelta(minutes=30),
            )
            session.add(credential)
            session.flush()
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
            assert adopted == unit.model_copy(
                update={"workspace_id": namespace.id, "provider_connection_id": None}
            )
            old_worker = TokenRepository(session).get_across_workspaces(token.id)
            retained_human = TokenRepository(session).get_across_workspaces(human.id)
            assert old_worker is not None and old_worker.status is TokenStatus.Revoked
            assert retained_human is not None and retained_human.status is TokenStatus.Active
            row = session.execute(
                text(
                    "SELECT workspace_id::text,user_id,status "
                    "FROM compute_join_credentials WHERE id=:id"
                ),
                {"id": credential.id},
            ).one()
            assert tuple(row) == (namespace.id, None, "revoked")
            assert session.scalar(text("SELECT count(*) FROM aws_account_connections")) == 0
    finally:
        database.dispose()

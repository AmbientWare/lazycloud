from collections.abc import Callable
from datetime import timedelta
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from database.migrations import alembic_config
from database.repositories.compute import ComputeUnitRepository
from database.repositories.identity import TokenRepository, UserRepository, WorkspaceRepository
from database.tables import DatabaseBase
from database.tables.compute import ComputeJoinCredentialTable, ComputeUnitTable
from database.tables.orchestration import WorkerTable
from identity.platform import PlatformNamespaceService
from provider_aws.platform import AwsPlatformBinding
from pydantic import SecretStr
from shared.aws_connections import AwsAccountNetwork
from shared.identity import TokenKind, TokenStatus, WorkspaceKind
from shared.placement import Placement
from shared.timestamps import utc_now
from sqlalchemy import Table, column, insert, table, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import CallableColumnDefault, ScalarElementColumnDefault
from sqlalchemy.types import TypeEngine

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
            # Written as the 0004 schema knows it: the ORM models carry columns
            # later revisions rename or drop.
            _insert_at_0004(
                session,
                ComputeUnitTable(
                    id=unit_id,
                    workspace_id=workspace_id,
                    name="existing-fleet",
                    placement="lazycloud",
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
                ),
                renames={"placement": "pool"},
                extra={"default_eligible": False},
            )
            session.execute(
                text(
                    "INSERT INTO machines (id, capacity_owner_id, workspace_id, pool, provider, "
                    "status, gpu_count, labels) VALUES (:id, :owner, :workspace, 'default', "
                    "'local', 'stopped', 0, '{}'::jsonb)"
                ),
                {"id": machine_id, "owner": unit_id, "workspace": workspace_id},
            )
            _insert_at_0004(
                session,
                WorkerTable(
                    id=worker_id,
                    machine_id=machine_id,
                    workspace_id=workspace_id,
                    placement="default",
                    status="stopped",
                    labels={},
                    last_seen_at=utc_now(),
                ),
                renames={"placement": "pool"},
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
            _insert_at_0004(
                session,
                ComputeJoinCredentialTable(
                    id=credential_id,
                    token_hash="0" * 64,
                    user_id=user.id,
                    workspace_id=workspace_id,
                    capacity_owner_id=unit_id,
                    placement="lazycloud",
                    created_by_token_id=None,
                    max_uses=1,
                    expires_at=utc_now() + timedelta(minutes=30),
                ),
                renames={"placement": "pool"},
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


def _insert_at_0004(
    session: Session,
    row: DatabaseBase,
    *,
    renames: dict[str, str],
    extra: dict[str, object] | None = None,
) -> None:
    """Insert an ORM row through the column names an older schema used."""
    mapped_table = cast(Table, row.__table__)
    values: dict[str, object] = {}
    types: dict[str, TypeEngine[object]] = {}
    for mapped in mapped_table.columns:
        value = getattr(row, mapped.name)
        if value is None and isinstance(mapped.default, ScalarElementColumnDefault):
            value = mapped.default.arg
        if value is None and isinstance(mapped.default, CallableColumnDefault):
            value = cast(Callable[[object], object], mapped.default.arg)(None)
        if value is None:
            # Nullable, server-defaulted, or generated at flush time; the old
            # schema fills it the same way the ORM would have.
            continue
        name = renames.get(mapped.name, mapped.name)
        values[name] = value
        types[name] = mapped.type
    columns = [column(name, types[name]) for name in values]
    columns.extend(column(name) for name in (extra or {}))
    target = table(mapped_table.name, *columns)
    session.execute(insert(target).values(**values, **(extra or {})))

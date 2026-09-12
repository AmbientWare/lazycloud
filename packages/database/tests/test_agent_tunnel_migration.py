from __future__ import annotations

from uuid import uuid4

from alembic import command
from database.migrations import alembic_config, bootstrap_database
from database.repositories.compute import (
    ComputeMachineEnrollmentCreate,
    ComputeMachineEnrollmentRepository,
    ComputeUnitRepository,
)
from database.repositories.orchestration import MachineRepository
from database.tables.apps import StubTable
from database.tables.compute import ComputeMachineEnrollmentTable
from database.tables.identity import UserTable, WorkspaceTable
from pydantic import JsonValue, TypeAdapter
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource, UnitName
from shared.compute_fleet import Machine
from shared.compute_policy import ComputeUnitRecord, MachinePool
from shared.timestamps import utc_now
from sqlalchemy import MetaData, Table, create_engine, insert, inspect, select, update
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session


def test_tunnel_cutover_preserves_enrollment_authority_and_customer_configuration(
    postgres_database_url: URL,
) -> None:
    url = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(url), "0044_stub_preparation_identity")
    engine = create_engine(url)
    user_id, workspace_id, machine_id, unit_id, stub_id = (str(uuid4()) for _ in range(5))
    now = utc_now()
    unit = ComputeUnitRecord(
        id=unit_id,
        workspace_id=workspace_id,
        capacity_owner_id=unit_id,
        capacity_owner_kind=CapacityOwnerKind.WorkspaceAgent,
        capacity_owner_source=CapacityOwnerSource.Agent,
        name=UnitName("owner-unit"),
        pool=MachinePool("owner-pool"),
        config={"selector": "owner-choice"},
    )
    payload = TypeAdapter(dict[str, JsonValue]).validate_json(unit.model_dump_json())
    legacy_payload: dict[str, JsonValue] = {
        **payload,
        "transport": "private_network",
        "config": {"selector": "owner-choice", "transport": "private_network"},
    }
    customer_payload: dict[str, JsonValue] = {
        "id": stub_id,
        "metadata": {"transport": "customer-label", "network_phase": "customer-value"},
    }
    try:
        old_units = Table("compute_units", MetaData(), autoload_with=engine)
        with engine.begin() as connection:
            connection.execute(insert(UserTable).values(id=user_id))
            connection.execute(
                insert(WorkspaceTable).values(id=workspace_id, name="owner", payload={})
            )
            connection.execute(
                old_units.insert().values(
                    **{
                        column.name: legacy_payload[column.name]
                        for column in old_units.columns
                        if column.name in legacy_payload
                    },
                    payload=legacy_payload,
                )
            )
            connection.execute(
                insert(StubTable).values(
                    id=stub_id,
                    workspace_id=workspace_id,
                    name="customer-handler",
                    type="function",
                    payload=customer_payload,
                )
            )
        with Session(engine) as session:
            MachineRepository(session).upsert(
                Machine(id=machine_id, capacity_owner_id=unit_id), workspace_id=workspace_id
            )
            enrollments = ComputeMachineEnrollmentRepository(session)
            enrollment = enrollments.create(
                ComputeMachineEnrollmentCreate(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    capacity_owner_id=unit_id,
                    pool=unit.pool,
                    machine_id=machine_id,
                    machine_fingerprint_hash="a" * 64,
                    credential_hash="b" * 64,
                    credential_generation=9,
                    last_join_at=now,
                )
            )
            expected = enrollments.save(
                enrollment.model_copy(update={"tunnel_public_key_sha256": "c" * 64})
            )
            legacy_enrollment = TypeAdapter(dict[str, JsonValue]).validate_json(
                expected.model_dump_json()
            )
            legacy_enrollment.update(
                network_generation=7,
                network_phase="connected",
                network_peer_id=str(uuid4()),
                network_public_key="retired-key",
                network_address="100.96.1.1/32",
                network_verified_at=now.isoformat(),
                network_failure_detail="retired-detail",
            )
            session.execute(
                update(ComputeMachineEnrollmentTable)
                .where(ComputeMachineEnrollmentTable.id == enrollment.id)
                .values(payload=legacy_enrollment)
            )
            session.commit()

        bootstrap_database(url)
        with Session(engine) as session:
            observed = ComputeMachineEnrollmentRepository(session).by_id(
                enrollment.id, workspace_id=workspace_id
            )
            assert observed == expected
            restored_unit = ComputeUnitRepository(session).get(unit_id)
            assert restored_unit is not None
            assert restored_unit.config == unit.config
            assert restored_unit.capacity_owner_id == unit_id
            assert session.scalar(select(StubTable.payload)) == customer_payload
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert {"wireguard_peers", "wireguard_gateway"}.isdisjoint(inspector.get_table_names())
            assert "transport" not in {
                column["name"] for column in inspector.get_columns("compute_units")
            }
    finally:
        engine.dispose()

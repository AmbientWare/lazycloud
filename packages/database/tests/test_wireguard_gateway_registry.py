from __future__ import annotations

import base64

import pytest
from alembic import command
from database.migrations import alembic_config, bootstrap_database
from database.repositories.compute import (
    PRIMARY_WIREGUARD_GATEWAY_ID,
    WireGuardGatewayRepository,
    wireguard_gateway_id,
)
from database.tables.compute import WireGuardGatewayTable
from shared.compute_enrollment import WireGuardGateway
from shared.errors import ConflictError
from shared.timestamps import utc_now
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session


def test_gateway_migration_preserves_enrolled_identity_and_rejects_collisions(
    postgres_database_url: URL,
) -> None:
    database_url = postgres_database_url.render_as_string(hide_password=False)
    command.upgrade(alembic_config(database_url), "0042_worker_release_ownership")
    primary = WireGuardGateway(
        id=PRIMARY_WIREGUARD_GATEWAY_ID,
        public_key=base64.b64encode(bytes(range(32))).decode(),
        endpoint="gateway.example:51820",
        updated_at=utc_now(),
    )
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO wireguard_gateway (id, public_key, endpoint, payload) "
                    "VALUES (:id, :public_key, :endpoint, CAST(:payload AS JSONB))"
                ),
                {
                    "id": primary.id,
                    "public_key": primary.public_key,
                    "endpoint": primary.endpoint,
                    "payload": primary.model_dump_json(exclude={"index"}),
                },
            )

        bootstrap_database(database_url)

        with Session(engine) as session, session.begin():
            repository = WireGuardGatewayRepository(session)
            assert repository.current() == primary
            secondary = repository.save(
                WireGuardGateway(
                    id=wireguard_gateway_id(1),
                    index=1,
                    public_key=base64.b64encode(bytes(reversed(range(32)))).decode(),
                    endpoint="gateway-1.example:51820",
                    updated_at=utc_now(),
                )
            )
            relocated = primary.model_copy(update={"endpoint": "gateway-0.example:51820"})
            assert repository.save(relocated) == relocated
            assert repository.list_all() == [relocated, secondary]
            assert repository.get_by_index(1) == secondary
            assert session.scalar(
                select(WireGuardGatewayTable.payload).where(WireGuardGatewayTable.id == primary.id)
            ) == relocated.model_dump(mode="json", exclude={"index"})

            with pytest.raises(ConflictError, match="cannot be changed"):
                repository.save(primary.model_copy(update={"public_key": secondary.public_key}))
            with pytest.raises(ConflictError, match="already registered"):
                repository.save(
                    secondary.model_copy(update={"id": wireguard_gateway_id(2), "index": 2})
                )
            with pytest.raises(ConflictError, match="identity does not match"):
                repository.save(secondary.model_copy(update={"id": primary.id}))

            assert repository.list_all() == [relocated, secondary]
    finally:
        engine.dispose()

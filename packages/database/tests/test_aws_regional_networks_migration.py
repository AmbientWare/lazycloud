from __future__ import annotations

from uuid import uuid4

from alembic import command
from database.migrations import alembic_config
from database.tables.compute import AwsAccountConnectionTable
from database.tables.identity import UserTable
from pydantic import JsonValue
from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import URL


def test_existing_networks_keep_their_region_and_other_connection_facts(
    postgres_database_url: URL,
) -> None:
    url = postgres_database_url.render_as_string(hide_password=False)
    config = alembic_config(url)
    command.upgrade(config, "0031_artifact_plan_retention")
    engine = create_engine(url)
    network: dict[str, JsonValue] = {
        "vpc_id": "vpc-01234567",
        "subnet_ids": ["subnet-01234567", "subnet-89abcdef"],
        "security_group_id": "sg-01234567",
    }
    try:
        with engine.begin() as connection:
            for name, region, known in (
                ("existing-role", "us-east-1", True),
                ("managed-stack", "us-west-2", True),
                ("awaiting-stack", "us-east-1", False),
            ):
                owner = str(uuid4())
                connection.execute(insert(UserTable).values(id=owner))
                payload: dict[str, JsonValue] = {
                    "marker": name,
                    "network": network if known else None,
                    "active_authorization": {
                        "managed_authorization": {"region": region}
                        if name == "managed-stack"
                        else None,
                    },
                }
                connection.execute(
                    insert(AwsAccountConnectionTable).values(
                        id=str(uuid4()),
                        user_id=owner,
                        account_id="123456789012",
                        external_id=str(uuid4()),
                        phase="ready",
                        payload=payload,
                    )
                )
        command.upgrade(config, "0032_aws_regional_networks")
        with engine.connect() as connection:
            rows = connection.execute(select(AwsAccountConnectionTable.payload)).scalars().all()
        by_name = {row["marker"]: row for row in rows}
        assert by_name["existing-role"]["networks"] == {"us-east-1": network}
        assert by_name["managed-stack"]["networks"] == {"us-west-2": network}
        assert by_name["awaiting-stack"]["networks"] == {}
        assert by_name["managed-stack"]["active_authorization"] == {
            "managed_authorization": {"region": "us-west-2"},
        }
        assert all("network" not in row for row in rows)
    finally:
        engine.dispose()

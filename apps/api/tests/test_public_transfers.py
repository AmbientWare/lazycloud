from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from api.server.public_transfers import PublicTransferMiddleware, attribute_public_transfer
from database.context import ServiceContext
from database.repositories.billing_rates import PlatformRateRepository
from database.tables.billing_ledger import BillingLedgerSegmentTable
from database.tables.observability import UsageRecordTable
from observability.usage import UsageService
from shared.timestamps import utc_now
from shared.usage import UsageMetric
from sqlalchemy import select
from sqlalchemy.engine import URL
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from tests.service_fixtures import postgres_database_url, unfunded_billing_account

from database import AsyncDatabaseClient, DatabaseApplicationName, DatabaseClient, DatabaseSettings


def test_only_verified_public_response_bytes_reach_charges(
    postgres_database_url: URL, tmp_path: Path
) -> None:
    settings = DatabaseSettings(
        url=postgres_database_url.render_as_string(hide_password=False),
        application_name=DatabaseApplicationName.Test,
    )
    database = DatabaseClient.from_settings(settings)
    context = ServiceContext.create(database, root=tmp_path)
    now = utc_now()
    _, workspace_id = unfunded_billing_account(
        context, period_started_at=now, period_ended_at=now + timedelta(days=30)
    )
    payload = b"a returned artifact\n"
    with database.session() as session:
        PlatformRateRepository(session).publish(
            pricing_version="public-transfer-acceptance",
            effective_at=utc_now() - timedelta(minutes=1),
            nanos_per_egress_byte=Decimal(1),
            nanos_per_volume_byte_second=Decimal(0),
        )

    async def exercise() -> None:
        async_database = AsyncDatabaseClient.from_settings(settings)
        usage = UsageService(context, async_database=async_database)

        async def download(request: Request) -> Response:
            attribute_public_transfer(
                request,
                workspace_id=workspace_id,
                resource_type="artifact",
                resource_id="download",
            )
            return Response(payload)

        app = Starlette(routes=[Route("/", download)])
        app.add_middleware(PublicTransferMiddleware, usage=lambda: usage, client_ip_header="")
        try:
            for address in ("8.8.8.8", "10.0.0.2"):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, client=(address, 12345)),
                    base_url="http://localhost",
                ) as client:
                    response = await client.get("/", headers={"X-Forwarded-For": "8.8.8.8"})
                    assert response.content == payload
            with database.session() as session:
                records = session.scalars(select(UsageRecordTable)).all()
                assert {(row.metric, row.quantity) for row in records} == {
                    (UsageMetric.NetworkEgressBytes.value, len(payload)),
                    (UsageMetric.NetworkSentBytes.value, len(payload)),
                }
                segments = session.scalars(select(BillingLedgerSegmentTable)).all()
                assert len(segments) == 1
                assert segments[0].cost_nanos == len(payload)
                assert segments[0].workspace_id == workspace_id
        finally:
            await async_database.dispose()

    try:
        asyncio.run(exercise())
    finally:
        database.dispose()


__all__ = ["postgres_database_url"]

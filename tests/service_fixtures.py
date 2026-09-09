from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from agent.binary import AgentBinarySettings
from api.server.async_io import ApiAsyncIo
from api.server.services import ApiServices
from control.service import ControlPlaneService
from coordination.redis_client import RedisClient, RedisSettings
from database.context import ServiceContext
from database.repositories.billing import BillingAccountRepository
from database.repositories.billing_allowance import BillingAllowanceRepository
from database.repositories.identity import (
    UserRepository,
    WorkspaceMemberRepository,
    WorkspaceRepository,
)
from execution.collections.redis import (
    RedisMapService,
    RedisSimpleQueueService,
)
from identity.auth import TokenIssuer
from identity.users import UserService
from pydantic import JsonValue
from shared.billing_accounts import BillingAccountStatus
from shared.billing_plans import BillingPlanId
from shared.billing_rate_card import FREE_PLAN_INCLUDED_NANOS
from shared.identity import (
    AuthTokenRecord,
    PlatformRole,
    TokenKind,
    WorkspaceRecord,
    WorkspaceStorageConfig,
)
from shared.timestamps import utc_now
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from storage.volume_filesystem import LocalVolumeFilesystem
from storage_client.s3 import S3ObjectStoreSettings

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings
from tests.backing_services import postgres_url
from tests.fakes import FakeObjectClient
from tests.real_redis import RealRedisActors


@pytest.fixture
def postgres_database_url() -> Iterator[URL]:
    base_url = postgres_url()
    database_name = f"lazycloud_test_{uuid4().hex}"
    database_url = base_url.set(database=database_name)
    admin = create_engine(
        base_url,
        connect_args={"application_name": DatabaseApplicationName.Test.value},
    )
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"CREATE DATABASE {identifier}"))
        yield database_url
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            identifier = connection.dialect.identifier_preparer.quote_identifier(database_name)
            connection.execute(text(f"DROP DATABASE IF EXISTS {identifier} WITH (FORCE)"))
            remaining = connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :database_name"),
                {"database_name": database_name},
            )
            assert remaining == 0
        admin.dispose()


def _fixture_account(database: DatabaseClient, display_name: str) -> str:
    """An account as sign-in leaves one: a user, and a billing account behind it.

    Both, because production has no account that holds one without the other.
    Signing in registers the customer, subscribes them to the free plan and grants
    what it includes before the session exists, and admission refuses an account
    whose usage would reach no invoice — so a fixture that created only the user
    would build a workspace nothing in it may run anything in.

    The provider identifiers are this repository's own rather than a payment
    provider's, which is the one thing here that is not what production wrote.
    Nothing offline can register a real customer, and what every caller of this
    reads is the durable row rather than the objects it names.
    """

    with database.session() as session:
        user_id = UserRepository(session).create(display_name=display_name).id
        BillingAccountRepository(session).upsert(
            user_id=user_id,
            status=BillingAccountStatus.Active,
            provider_customer_id=f"cus_fixture_{user_id}",
            provider_subscription_id=f"sub_fixture_{user_id}",
            provider_credit_grant_id=f"credgr_fixture_{user_id}",
            plan=BillingPlanId.Free,
        )
        # The cycle provisioning opens alongside the subscription. Both or
        # neither: an account holding a subscription with no allowance period is
        # a shape production never writes, and admission reads it as an account
        # with nothing left to spend — so every test that starts a container
        # would be refused for a state the fixture invented.
        #
        # Opened far enough back that a test writing its own cycle over this one
        # is unambiguously the later of the two. Where periods overlap the most
        # recently begun takes the answer, and a fixture cycle starting near now
        # would win against a test's by a margin measured in whichever ran first.
        now = utc_now()
        BillingAllowanceRepository(session).set_subscription_period(
            user_id=user_id,
            period_started_at=now - timedelta(days=365),
            period_ended_at=now + timedelta(days=30),
            allowance_nanos=FREE_PLAN_INCLUDED_NANOS,
            funded=False,
        )
        return user_id


@dataclass(slots=True)
class _InMemoryWorkspaceBuckets:
    """Provision workspace storage without reaching an object store.

    Workspace storage is mandatory in production, so a workspace created here
    must have it too, or every request that mounts one fails. Only bucket
    creation is faked; the rest of the flow is the production path.
    """

    settings: S3ObjectStoreSettings = field(default_factory=S3ObjectStoreSettings)
    created: list[str] = field(default_factory=list)

    def create_bucket(self, bucket: str | None = None) -> None:
        self.created.append(bucket or self.settings.bucket)

    def validate_bucket_access(self, bucket: str | None = None) -> None:
        del bucket

    def configure_workspace_bucket(self, bucket: str, *, public_origin: str) -> None:
        del bucket, public_origin


@contextmanager
def service_graph(
    database: DatabaseClient,
    tmp_path: Path,
    *,
    redis_client: RedisClient,
    binary_redis_client: RedisClient,
    async_io: ApiAsyncIo,
) -> Iterator[ApiServices]:
    """The production service graph over the database and Redis handed in.

    Separate from the fixture so a test needing a real PostgreSQL backend gets
    the same wiring rather than assembling its own. Only the backends differ.
    Everything a workspace needs before it can run anything, the account and
    provisioned storage, is set up here, because a graph missing any of it
    refuses work for a reason the test did not intend. The caller owns
    `async_io` and closes it on the loop that used it.
    """

    maps = RedisMapService(binary_redis_client)
    simple_queues = RedisSimpleQueueService(binary_redis_client)
    volume_filesystem = LocalVolumeFilesystem(tmp_path / "volumes")

    services = ApiServices.create(
        database,
        root=tmp_path,
        redis_client=redis_client,
        binary_redis_client=binary_redis_client,
        async_io=async_io,
        owns_redis_client=False,
        owns_binary_redis_client=False,
        map_service=maps,
        simple_queue_service=simple_queues,
        volume_filesystem=volume_filesystem,
        # Without this the graph wires a real S3 client at the configured
        # endpoint, so a unit test that touches object storage reaches out over
        # the network instead of failing on its own terms.
        object_store_client=FakeObjectClient(),
        workspace_storage_client=_InMemoryWorkspaceBuckets(),
        agent_binary_settings=AgentBinarySettings(
            binary_dir=tmp_path,
            binary_version="test",
            binary_sha256_by_arch={"amd64": "a" * 64},
        ),
    )
    services.control_plane_service.set_workspace(
        "default",
        owner_user_id=_fixture_account(services.context.database, "default-workspace-owner"),
    )
    services.control_plane_service.ensure_workspace_storage("default")
    try:
        yield services
    finally:
        services.close()


@pytest.fixture
def isolated_services(
    tmp_path: Path,
    real_redis_actors: RealRedisActors,
) -> Iterator[ApiServices]:
    database_path = tmp_path / "services.sqlite3"
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=f"sqlite+pysqlite:///{database_path}",
            application_name=DatabaseApplicationName.Test,
        )
    )
    redis = real_redis_actors.client()
    binary_redis = real_redis_actors.client(decode_responses=False)
    async_io = ApiAsyncIo.from_settings(
        DatabaseSettings(
            url=f"sqlite+aiosqlite:///{database_path}",
            application_name=DatabaseApplicationName.Test,
        ),
        RedisSettings(
            url=real_redis_actors.url,
            key_prefix=real_redis_actors.prefix,
            socket_timeout_seconds=2.0,
            health_check_interval_seconds=1,
        ),
    )
    with service_graph(
        database,
        tmp_path,
        redis_client=redis,
        binary_redis_client=binary_redis,
        async_io=async_io,
    ) as services:
        yield services


def owned_workspace(
    control: ControlPlaneService,
    name: str = "default",
    *,
    storage: WorkspaceStorageConfig | None = None,
    labels: dict[str, str] | None = None,
    metadata: Mapping[str, JsonValue] | None = None,
) -> WorkspaceRecord:
    """A workspace with the owner row production writes in the same transaction.

    Tests that only need a workspace to exist go through here rather than writing the
    row alone: everything a workspace resolves through its account—compute, domains,
    credentials—needs that row, and a workspace without one exists nowhere else.
    """
    owner_user_id = _existing_owner(control, name) or _fixture_account(
        control.context.database,
        f"{name}-owner-{uuid4().hex[:8]}",
    )
    return control.set_workspace(
        name,
        owner_user_id=owner_user_id,
        storage=storage,
        labels=labels,
        metadata=metadata,
    )


def _existing_owner(control: ControlPlaneService, name: str) -> str | None:
    with control.context.database.session() as session:
        record = WorkspaceRepository(session).by_name(name)
        if record is None:
            return None
        owner = WorkspaceMemberRepository(session).owner(record.id)
    return owner.user_id if owner is not None else None


def unbilled_account(context: ServiceContext) -> tuple[str, str]:
    """A workspace and its owner, with nothing billing has ever written.

    The opposite of what `_fixture_account` leaves, and the state the billing
    tests need: what provisioning does on first reaching an account cannot be
    observed against one a fixture has already stood a row up for.
    """

    with context.database.session() as session:
        user_id = UserRepository(session).create(display_name="unprovisioned").id
        workspace_id = WorkspaceRepository(session).create(name=f"unbilled-{uuid4()}").id
        WorkspaceMemberRepository(session).ensure_owner(workspace_id=workspace_id, user_id=user_id)
    return user_id, workspace_id


def carded_account(context: ServiceContext) -> tuple[str, str]:
    """An unprovisioned workspace and owner whose account already holds a card.

    What a plan's own terms can only be observed against. An account with no card
    is given what the platform will spend to find out whether it can bill anybody,
    whatever plan it is on — so a test that put an account on Team and read back
    the plan's allowance would be reading the cardless figure and calling it a
    plan.

    The row is written before provisioning rather than after, because
    provisioning is what buys the first grant and a card attached afterwards
    would be a cycle already funded at the wrong figure.
    """

    user_id, workspace_id = unbilled_account(context)
    with context.database.session() as session:
        accounts = BillingAccountRepository(session)
        accounts.lock_for_registration(user_id)
        accounts.set_payment_method_present(user_id=user_id, present=True, at=utc_now())
        session.commit()
    return user_id, workspace_id


def workspace_owner_user_id(context: ServiceContext, workspace_id: str) -> str:
    """The account that owns a workspace, created on first ask.

    Production writes the owner row with the workspace, so anything resolving compute
    or domains through the account finds one. Tests that build a workspace through a
    lower-level path need the same row before they can join a machine to it.
    """
    with context.database.session() as session:
        existing = WorkspaceMemberRepository(session).owner(workspace_id)
    if existing is not None:
        return existing.user_id
    user_id = _fixture_account(context.database, f"owner-{uuid4().hex[:12]}")
    with context.database.session() as session:
        WorkspaceMemberRepository(session).ensure_owner(
            workspace_id=workspace_id,
            user_id=user_id,
        )
    return user_id


def administrator_credential(
    services: ApiServices,
    name: str = "administrator",
) -> tuple[str, AuthTokenRecord]:
    """An administrator credential, made the only way production makes one.

    Administrator standing belongs to the account, not to the shape of a token, so
    this creates a user whose role says so and mints a credential naming them. No
    membership: a platform administrator reaches every workspace without one, and
    granting it would collide with whatever owner the test set up itself.
    """
    user = UserService(services.context).create(
        display_name=f"admin-{uuid4().hex[:12]}",
        role=PlatformRole.Administrator,
    )
    issuer = TokenIssuer(services.context)
    with services.context.database.session() as session:
        raw_token, record = issuer.issue_for_user(
            session,
            name,
            user_id=user.id,
            kind=TokenKind.Admin,
        )
    issuer.committed()
    return raw_token, record

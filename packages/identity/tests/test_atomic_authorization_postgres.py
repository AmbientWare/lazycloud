from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from database.context import ServiceContext
from database.recovery import ControlPlaneRecoveryFence
from database.repositories.identity import (
    DeviceAuthorizationRepository,
    TokenRepository,
    WorkspaceAuditRepository,
    WorkspaceRepository,
)
from database.tables.identity import TokenTable
from identity.auth import AuthError, AuthService
from identity.credential_files import CredentialFilePublication
from identity.device_auth import DeviceAuthorizationService
from shared.errors import ConflictError
from shared.http.workspaces import WorkspaceAuditAction
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@contextmanager
def _postgres_test_schema() -> Iterator[str]:
    database_url = os.environ.get("LAZYCLOUD_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("LAZYCLOUD_TEST_POSTGRES_URL is required for PostgreSQL concurrency proof")

    schema = f"atomic_auth_{uuid4().hex}"
    admin_engine = create_engine(database_url)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
    finally:
        admin_engine.dispose()

    try:
        isolated_url = (
            make_url(database_url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
        yield isolated_url
    finally:
        _drop_postgres_test_schema(database_url, schema)


@contextmanager
def _postgres_test_context(
    tmp_path: Path,
) -> Iterator[tuple[ServiceContext, DatabaseClient]]:
    with _postgres_test_schema() as isolated_url:
        database = DatabaseClient.from_settings(
            DatabaseSettings(
                url=isolated_url,
                pool_size=12,
                max_overflow=0,
                statement_timeout_ms=0,
                application_name=DatabaseApplicationName.Test,
            )
        )
        try:
            database.create_schema()
            yield ServiceContext.create(database, root=tmp_path, create_schema=False), database
        finally:
            database.dispose()


def _drop_postgres_test_schema(database_url: str, schema: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
    finally:
        engine.dispose()


def test_postgresql_authorization_claims_are_atomic(tmp_path: Path) -> None:
    with _postgres_test_context(tmp_path) as (context, database):
        bootstrap_barrier = Barrier(8)

        def bootstrap(index: int) -> tuple[bool, str]:
            bootstrap_barrier.wait(timeout=10)
            try:
                result = AuthService(context).bootstrap_administrator(
                    request_id=f"bootstrap:concurrent-{index}",
                    name=f"admin-{index}",
                )
            except AuthError:
                return False, ""
            return True, result.record.user_id

        with ThreadPoolExecutor(max_workers=8) as executor:
            bootstrap_results = list(executor.map(bootstrap, range(8)))
        bootstrap_winners = [item for item in bootstrap_results if item[0]]
        assert len(bootstrap_winners) == 1
        assert sum(not item[0] for item in bootstrap_results) == 7
        user_id = bootstrap_winners[0][1]
        with database.session() as session:
            assert len(TokenRepository(session).list_across_workspaces()) == 1

        devices = DeviceAuthorizationService(context)
        decision = devices.start(client_name="decision-race")
        decision_barrier = Barrier(2)

        def approve() -> str:
            decision_barrier.wait(timeout=10)
            try:
                devices.approve(decision.record.user_code, user_id=user_id)
            except ConflictError:
                return "conflict"
            return "approved"

        def deny() -> str:
            decision_barrier.wait(timeout=10)
            try:
                devices.deny(decision.record.user_code)
            except ConflictError:
                return "conflict"
            return "denied"

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (executor.submit(approve), executor.submit(deny))
            decision_results = [future.result(timeout=15) for future in futures]
        assert decision_results.count("conflict") == 1
        assert sum(result in {"approved", "denied"} for result in decision_results) == 1

        claim = devices.start(client_name="claim-race")
        devices.approve(claim.record.user_code, user_id=user_id)
        with database.session() as session:
            token_count_before = len(TokenRepository(session).list_across_workspaces())
        claim_barrier = Barrier(8)

        def consume() -> tuple[bool, str]:
            claim_barrier.wait(timeout=10)
            try:
                result = devices.claim(claim.device_code)
            except ConflictError:
                return False, ""
            return True, result.token

        def consume_attempt(_index: int) -> tuple[bool, str]:
            return consume()

        with ThreadPoolExecutor(max_workers=8) as executor:
            claim_results = list(executor.map(consume_attempt, range(8)))
        claim_winners = [item for item in claim_results if item[0]]
        assert len(claim_winners) == 1
        assert sum(not item[0] for item in claim_results) == 7
        assert claim_winners[0][1]
        with database.session() as session:
            assert len(TokenRepository(session).list_across_workspaces()) == token_count_before + 1
            retained = DeviceAuthorizationRepository(session).by_user_code(claim.record.user_code)
        assert retained is not None
        assert retained.consumed_at is not None
        AuthService(context).authenticate(claim_winners[0][1])


def test_postgresql_non_reusable_token_has_exactly_one_authenticated_claimant(
    tmp_path: Path,
) -> None:
    with _postgres_test_context(tmp_path) as (context, database):
        with database.session() as session:
            WorkspaceRepository(session).ensure_named("default")
        raw_token, created = AuthService(context).create_token(
            "single-use-concurrency",
            reusable=False,
        )
        barrier = Barrier(8)

        def authenticate(_index: int) -> tuple[bool, str]:
            barrier.wait(timeout=10)
            try:
                record = AuthService(context).authenticate(raw_token)
            except AuthError:
                return False, ""
            return True, record.id

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(authenticate, range(8)))

        winners = [token_id for succeeded, token_id in results if succeeded]
        assert winners == [created.id]
        assert sum(not succeeded for succeeded, _token_id in results) == 7
        with database.session() as session:
            row = session.scalar(select(TokenTable).where(TokenTable.id == created.id))
            mapped = TokenRepository(session).get(
                created.id,
                workspace_id=created.workspace_id,
            )
        assert row is not None
        assert row.consumed_at is not None
        assert row.last_used_at == row.consumed_at
        assert row.status == "revoked"
        assert mapped is not None
        assert mapped.status.value == "revoked"
        assert not mapped.reusable


def test_postgresql_offline_recovery_requires_stopped_control_plane_and_replays(
    tmp_path: Path,
) -> None:
    with _postgres_test_context(tmp_path) as (context, database):
        auth = AuthService(context)
        auth.bootstrap_administrator(
            request_id="bootstrap:postgres-recovery-owner",
        )
        serving_fence = ControlPlaneRecoveryFence(database)
        serving_fence.start_serving()
        try:
            with ControlPlaneRecoveryFence(database).offline_recovery() as acquired:
                assert not acquired
        finally:
            serving_fence.stop_serving()

        publication = CredentialFilePublication(
            tmp_path / "postgres-recovery-token",
            "recovery:postgres-incident-001",
        )
        with ControlPlaneRecoveryFence(database).offline_recovery() as acquired:
            assert acquired
            created = auth.recover_admin_token(
                request_id="postgres-incident-001",
                stage_token=publication.stage,
            )
            staged = publication.read_staged()
            assert staged is not None
            replay = auth.recover_admin_token(
                request_id="postgres-incident-001",
                staged_token=staged,
            )
            publication.publish(replace=False)
            auth.mark_admin_token_published(
                request_id="postgres-incident-001",
                recovery=True,
            )

        assert replay.replayed
        assert replay.record.id == created.record.id
        assert auth.authenticate(publication.read_published() or "").id == created.record.id
        with database.session() as session:
            # The workspace the recovery resolved, not the token's own: an
            # administrator credential names an account rather than a workspace,
            # so its `workspace_id` is empty and the audit it wrote is not there.
            audits = WorkspaceAuditRepository(session).page(
                workspace_id=context.default_workspace_id(session),
                limit=20,
            )
        assert (
            sum(
                audit.action is WorkspaceAuditAction.AdministratorRecovered
                for audit in audits.records
            )
            == 1
        )

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from secrets import token_urlsafe
from threading import Barrier
from uuid import uuid4

import pytest
from compute.offers import ComputeOffer
from compute.provider_launches import ProviderNodeLaunchService
from compute.providers import ProviderUnitBootstrap, ProviderUnitRequest
from database.repositories.compute import ComputeUnitRepository
from database.repositories.identity import WorkspaceRepository
from database.repositories.provider_launches import ProviderNodeLaunchRepository
from execution.secrets.crypto import WorkspaceSecretCipher
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitPhase,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.errors import ConflictError, InvalidInputError, UpstreamUnavailableError
from shared.http.provider_nodes import ProviderNodeIdentityRequest
from shared.provider_config import ProviderKind
from shared.timestamps import utc_now
from sqlalchemy.engine import URL

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings, bootstrap_database


@dataclass(frozen=True)
class LaunchOwner:
    database: DatabaseClient
    service: ProviderNodeLaunchService
    unit: ComputeUnitRecord
    request: ProviderUnitRequest


@pytest.fixture
def launch_owner(postgres_database_url: URL) -> Iterator[LaunchOwner]:
    url = postgres_database_url.render_as_string(hide_password=False)
    bootstrap_database(url)
    database = DatabaseClient.from_settings(
        DatabaseSettings(
            url=url, application_name=DatabaseApplicationName.Test, pool_size=4, max_overflow=0
        )
    )
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name="launch-authority")
        unit_id = str(uuid4())
        unit = ComputeUnitRecord(
            id=unit_id,
            workspace_id=workspace.id,
            name=UnitName("launch-authority"),
            pool=MachinePool("lazycloud"),
            provider="hetzner",
            provider_ref="hetzner:test",
            platform_fleet=True,
            capacity_mode=ComputeCapacityMode.Pooled,
            visibility=ComputeUnitVisibility.Internal,
            capacity_owner_id=unit_id,
            capacity_owner_kind=CapacityOwnerKind.PooledProvider,
            capacity_owner_source=CapacityOwnerSource.Provider,
            region="fsn1",
            offer_id="fsn1:ccx13",
            capability_key="hetzner:fsn1:ccx13:amd64:runsc",
        )
        ComputeUnitRepository(session).upsert(unit)

    def cipher(workspace_id: str) -> WorkspaceSecretCipher:
        assert workspace_id == workspace.id
        return WorkspaceSecretCipher.from_workspace(workspace)

    request = ProviderUnitRequest(
        workspace_id=workspace.id,
        unit_id=unit.id,
        unit_name=unit.name,
        provider_ref=unit.provider_ref,
        provider_connection_id=None,
        generation=unit.generation,
        desired_machines=1,
        max_machines=1,
        offer=ComputeOffer(
            id=unit.offer_id,
            provider=unit.provider_ref,
            instance_type="ccx13",
            region=unit.region,
            capacity_mode=ComputeCapacityMode.Pooled,
        ),
        bootstrap=ProviderUnitBootstrap(
            control_plane_url="https://api.example.test",
            enrollment_request_id=unit.id,
            agent_version="test",
            agent_sha256="0" * 64,
            agent_binary_url="https://api.example.test/agent",
        ),
    )
    try:
        yield LaunchOwner(database, ProviderNodeLaunchService(database, cipher), unit, request)
    finally:
        database.engine.dispose()


def test_one_redemption_wins_and_consumed_token_cannot_take_over_node(
    launch_owner: LaunchOwner,
) -> None:
    owner = launch_owner
    launch = owner.service.prepare(owner.request, "node-slot")
    repeated = owner.service.prepare(owner.request, "node-slot")
    assert repeated.launch_id == launch.launch_id
    assert repeated.bootstrap_token == launch.bootstrap_token
    request = ProviderNodeIdentityRequest(
        enrollment_request_id=owner.unit.id,
        provider=ProviderKind.Hetzner,
        region=owner.unit.region,
        provider_instance_id="123",
        identity_proof_url="provider-bootstrap",
        launch_id=launch.launch_id,
        bootstrap_token=launch.bootstrap_token.get_secret_value(),
        node_agent_token=token_urlsafe(32),
    )
    with pytest.raises(UpstreamUnavailableError):
        owner.service.authorize(request, owner.unit)
    owner.service.bind(
        launch.launch_id,
        provider_ref=owner.unit.provider_ref,
        provider_instance_id="123",
        unit_id=owner.unit.id,
        server_name="node-slot",
        region=owner.unit.region,
        generation=owner.unit.generation,
    )
    with pytest.raises(InvalidInputError, match="must differ"):
        owner.service.authorize(
            request.model_copy(update={"node_agent_token": request.bootstrap_token}), owner.unit
        )
    owner.service.bind(
        launch.launch_id,
        provider_ref=owner.unit.provider_ref,
        provider_instance_id="123",
        unit_id=owner.unit.id,
        server_name="node-slot",
        region=owner.unit.region,
        generation=owner.unit.generation,
    )
    with pytest.raises(ConflictError):
        owner.service.bind(
            launch.launch_id,
            provider_ref=owner.unit.provider_ref,
            provider_instance_id="456",
            unit_id=owner.unit.id,
            server_name="node-slot",
            region=owner.unit.region,
            generation=owner.unit.generation,
        )
    rival = request.model_copy(update={"node_agent_token": token_urlsafe(32)})
    barrier = Barrier(2)

    def redeem(candidate: ProviderNodeIdentityRequest) -> bool:
        barrier.wait()
        try:
            owner.service.authorize(candidate, owner.unit)
        except (InvalidInputError, UpstreamUnavailableError):
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(redeem, (request, rival)))
    assert sorted(results) == [False, True]
    winner = request if results[0] else rival
    loser = rival if results[0] else request
    with owner.database.session() as session:
        row = ProviderNodeLaunchRepository(session).get(launch.launch_id, for_update=True)
        assert row is not None and row.bootstrap_token_ciphertext is None
        row.created_at = utc_now() - timedelta(hours=2)
        row.expires_at = utc_now() - timedelta(hours=1)
    owner.service.authorize(winner.model_copy(update={"bootstrap_token": ""}), owner.unit)
    with pytest.raises(InvalidInputError):
        owner.service.authorize(loser, owner.unit)
    with pytest.raises(InvalidInputError):
        owner.service.authorize(
            winner.model_copy(update={"provider_instance_id": "456"}), owner.unit
        )
    owner.service.revoke(launch.launch_id)
    with pytest.raises(InvalidInputError):
        owner.service.authorize(winner, owner.unit)


def test_expired_unredeemed_launch_cannot_be_reused_or_silently_rotated(
    launch_owner: LaunchOwner,
) -> None:
    owner = launch_owner
    launch = owner.service.prepare(owner.request, "expired-slot")
    owner.service.bind(
        launch.launch_id,
        provider_ref=owner.unit.provider_ref,
        provider_instance_id="123",
        unit_id=owner.unit.id,
        server_name="expired-slot",
        region=owner.unit.region,
        generation=owner.unit.generation,
    )
    with owner.database.session() as session:
        row = ProviderNodeLaunchRepository(session).get(launch.launch_id, for_update=True)
        assert row is not None
        row.created_at = utc_now() - timedelta(hours=2)
        row.expires_at = utc_now() - timedelta(hours=1)
    with pytest.raises(ConflictError):
        owner.service.prepare(owner.request, "expired-slot")
    request = ProviderNodeIdentityRequest(
        enrollment_request_id=owner.unit.id,
        provider=ProviderKind.Hetzner,
        region=owner.unit.region,
        provider_instance_id="123",
        identity_proof_url="provider-bootstrap",
        launch_id=launch.launch_id,
        bootstrap_token=launch.bootstrap_token.get_secret_value(),
        node_agent_token=token_urlsafe(32),
    )
    with pytest.raises(InvalidInputError):
        owner.service.authorize(request, owner.unit)
    owner.service.revoke(launch.launch_id)
    replacement = owner.service.prepare(owner.request, "expired-slot")
    assert replacement.launch_id != launch.launch_id


def test_only_one_creation_attempt_survives_concurrent_reconciliation(
    launch_owner: LaunchOwner,
) -> None:
    owner = launch_owner
    launch = owner.service.prepare(owner.request, "create-slot")
    barrier = Barrier(2)

    def attempt() -> bool:
        barrier.wait()
        try:
            return owner.service.mark_creation_attempt(launch.launch_id)
        except UpstreamUnavailableError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = [executor.submit(attempt) for _ in range(2)]
        assert sum(attempt.result(timeout=2) for attempt in attempts) == 1
    status = owner.service.for_server(owner.request, "create-slot")
    assert status is not None and status.creation_attempted
    restarted = ProviderNodeLaunchService(owner.database, owner.service.cipher_for_workspace)
    assert not restarted.mark_creation_attempt(launch.launch_id)
    assert restarted.prepare(owner.request, "create-slot").launch_id == launch.launch_id


def test_creation_operation_cannot_be_reassigned_to_another_launch(
    launch_owner: LaunchOwner,
) -> None:
    owner = launch_owner
    first = owner.service.prepare(owner.request, "first-slot")
    second = owner.service.prepare(owner.request, "second-slot")
    operation_id = str(uuid4())
    with pytest.raises(InvalidInputError):
        owner.service.record_creation_operation(first.launch_id, operation_id)
    assert owner.service.mark_creation_attempt(first.launch_id)
    assert owner.service.mark_creation_attempt(second.launch_id)
    owner.service.record_creation_operation(first.launch_id, operation_id)
    owner.service.record_creation_operation(first.launch_id, operation_id)
    with pytest.raises(ConflictError):
        owner.service.record_creation_operation(first.launch_id, str(uuid4()))
    with pytest.raises(ConflictError):
        owner.service.record_creation_operation(second.launch_id, operation_id)
    status = owner.service.for_server(owner.request, "first-slot")
    assert status is not None and status.provider_operation_id == operation_id


@pytest.mark.parametrize("deleting", [False, True])
def test_stale_launch_cannot_purchase_after_unit_generation_or_lifecycle_changes(
    launch_owner: LaunchOwner,
    deleting: bool,
) -> None:
    owner = launch_owner
    launch = owner.service.prepare(owner.request, "stale-slot")
    updated = owner.unit.model_copy(
        update={
            "phase": ComputeUnitPhase.Deleting if deleting else owner.unit.phase,
            "generation": owner.unit.generation if deleting else owner.unit.generation + 1,
        }
    )
    with owner.database.session() as session:
        ComputeUnitRepository(session).upsert(updated)
    with pytest.raises(InvalidInputError, match="creation authorization is unavailable"):
        owner.service.mark_creation_attempt(launch.launch_id)
    with pytest.raises(InvalidInputError, match="active platform unit"):
        owner.service.prepare(owner.request, "another-slot")
    status = owner.service.for_server(owner.request, "stale-slot")
    assert status is not None and not status.creation_attempted


def test_enrollment_launch_contention_fails_without_waiting_for_the_owner(
    launch_owner: LaunchOwner,
) -> None:
    owner = launch_owner
    launch = owner.service.prepare(owner.request, "bounded-slot")
    owner.service.bind(
        launch.launch_id,
        provider_ref=owner.unit.provider_ref,
        provider_instance_id="123",
        unit_id=owner.unit.id,
        server_name="bounded-slot",
        region=owner.unit.region,
        generation=owner.unit.generation,
    )
    request = ProviderNodeIdentityRequest(
        enrollment_request_id=owner.unit.id,
        provider=ProviderKind.Hetzner,
        region=owner.unit.region,
        provider_instance_id="123",
        identity_proof_url="provider-bootstrap",
        launch_id=launch.launch_id,
        bootstrap_token=launch.bootstrap_token.get_secret_value(),
        node_agent_token=token_urlsafe(32),
    )
    owner.service.authorize(request, owner.unit)
    with owner.database.session() as session:
        owner.service.lock_enrollment(session, request, owner.unit, machine_fingerprint="host")
        with ThreadPoolExecutor(max_workers=1) as executor:
            blocked = executor.submit(owner.service.authorize, request, owner.unit)
            with pytest.raises(UpstreamUnavailableError, match="in progress"):
                blocked.result(timeout=1)
    owner.service.authorize(request, owner.unit)

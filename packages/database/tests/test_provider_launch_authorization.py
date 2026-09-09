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

from database import DatabaseApplicationName, DatabaseClient, DatabaseSettings


@dataclass(frozen=True)
class LaunchOwner:
    database: DatabaseClient
    service: ProviderNodeLaunchService
    unit: ComputeUnitRecord
    request: ProviderUnitRequest


@pytest.fixture
def launch_owner(migrated_database_url: URL) -> Iterator[LaunchOwner]:
    url = migrated_database_url.render_as_string(hide_password=False)
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


def test_ended_unit_cannot_issue_new_launch_credentials(launch_owner: LaunchOwner) -> None:
    owner = launch_owner
    with owner.database.session() as session:
        repository = ComputeUnitRepository(session)
        unit = repository.get(owner.unit.id)
        assert unit is not None
        repository.upsert(unit.model_copy(update={"phase": ComputeUnitPhase.Deleting}))
    with pytest.raises(InvalidInputError, match="active platform unit"):
        owner.service.prepare(owner.request, "ended-slot")


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

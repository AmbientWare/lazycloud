from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import uuid4

from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.offers import ComputeOffer
from compute.providers import (
    ComputeProviderResolver,
    ProviderCapacityPhase,
    ProviderUnitBootstrap,
    ProviderUnitRequest,
    ProviderUnitSnapshot,
    ResolvedComputeProvider,
)
from compute.service import ComputeService
from control.service import ControlPlaneService
from database.repositories.compute import AwsAccountConnectionRepository, ComputeUnitRepository
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.aws_connections import (
    AwsAccountAuthorizationGeneration,
    AwsAccountAuthorizationMode,
    AwsAccountAuthorizationPhase,
    AwsAccountConnection,
    AwsAccountConnectionPhase,
)
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeResourceRequirements,
    ComputeUnitPhase,
    ComputeUnitProviderState,
    ComputeUnitRecord,
)
from shared.http.compute import UnitScaleResponse
from shared.identity import TokenKind

_CONNECTION_ID = "11111111-1111-4111-8111-111111111111"
_OFFER_ID = "us-east-1:i4i.xlarge"
_PROVIDER_REF = f"aws:{_CONNECTION_ID}"


@dataclass(slots=True)
class _CapacityOwnerMutations:
    active_owner_id: str = ""
    open_reservations: bool = False

    @contextmanager
    def mutation_lock(self, capacity_owner_id: str) -> Iterator[None]:
        assert not self.active_owner_id
        self.active_owner_id = capacity_owner_id
        try:
            yield
        finally:
            self.active_owner_id = ""

    def has_open_reservations(self, capacity_owner_id: str) -> bool:
        assert self.active_owner_id == capacity_owner_id
        return self.open_reservations


@dataclass(slots=True)
class _PooledProvider:
    desired_machines: int = 1
    capacity_calls: list[tuple[int, int]] = field(default_factory=list)

    def list_offers(self) -> Iterable[ComputeOffer]:
        return (
            ComputeOffer(
                id=_OFFER_ID,
                provider=_PROVIDER_REF,
                cloud="aws",
                instance_type="i4i.xlarge",
                region="us-east-1",
                cpu_millicores=4_000,
                memory_mb=32 * 1_024,
                storage_mb=200 * 1_024,
                available=10,
                capacity_mode=ComputeCapacityMode.Pooled,
                capability_key="aws:us-east-1:i4i.xlarge:amd64:runc",
                supports_scale_to_zero=True,
            ),
        )

    def ensure_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.describe_unit(request)

    def describe_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return ProviderUnitSnapshot(
            phase=ProviderCapacityPhase.Ready,
            resource_id="asg-pool-scale",
            desired_machines=self.desired_machines,
            max_machines=request.max_machines,
            observed_machines=self.desired_machines,
            provider_state=ComputeUnitProviderState(resource_id="asg-pool-scale"),
        )

    def set_unit_capacity(
        self,
        request: ProviderUnitRequest,
        *,
        desired_machines: int,
        max_machines: int,
    ) -> ProviderUnitSnapshot:
        self.capacity_calls.append((desired_machines, max_machines))
        self.desired_machines = desired_machines
        return self.describe_unit(request.model_copy(update={"max_machines": max_machines}))

    def release_machine(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
    ) -> ProviderUnitSnapshot:
        del provider_instance_id
        return self.describe_unit(request)

    def delete_unit(self, request: ProviderUnitRequest) -> ProviderUnitSnapshot:
        return self.describe_unit(request).model_copy(
            update={"phase": ProviderCapacityPhase.Deleted}
        )

    def machine_storage_destroyed(
        self,
        request: ProviderUnitRequest,
        provider_instance_id: str,
        storage_volume_ids: tuple[str, ...],
    ) -> bool:
        del request, provider_instance_id
        return bool(storage_volume_ids)


@dataclass(frozen=True, slots=True)
class _Resolver(ComputeProviderResolver):
    provider: _PooledProvider

    def list_providers(self, workspace_id: str) -> Iterable[ResolvedComputeProvider]:
        del workspace_id
        return (self._resolved(),)

    def resolve(self, workspace_id: str, provider_ref: str) -> ResolvedComputeProvider:
        del workspace_id
        if provider_ref != _PROVIDER_REF:
            raise KeyError(provider_ref)
        return self._resolved()

    def _resolved(self) -> ResolvedComputeProvider:
        return ResolvedComputeProvider(
            ref=_PROVIDER_REF,
            capacity_mode=ComputeCapacityMode.Pooled,
            connection_id=_CONNECTION_ID,
            pooled=self.provider,
        )


def test_pool_scale_is_workspace_scoped_and_idempotently_returns_durable_capacity(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    provider = _PooledProvider()
    mutations = _CapacityOwnerMutations()
    compute = ComputeService(
        isolated_services.context,
        provider_resolver=_Resolver(provider),
        pool_bootstrap_factory=_bootstrap,
        capacity_owner_mutations=mutations,
    )
    workspace_id = _seed_connection(isolated_services)
    pool = compute.prepare_pooled_capacity(
        workspace="default",
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        region="us-east-1",
        desired_machines=1,
        workspace_machine_limit=10,
        root_volume_gib=200,
    )
    services_with_compute = replace(isolated_services, compute=compute)
    gateway = replace(
        services_with_compute.gateway_service,
        services=services_with_compute,
        capacity_reservations=mutations,
    )
    services = replace(services_with_compute, gateway_service=gateway)
    raw_token, _record = AuthService(isolated_services.context).create_token(
        "pool-scale",
        kind=TokenKind.Admin,
    )
    client = client_stack.enter_context(TestClient(create_app(services)))
    headers = {"Authorization": f"Bearer {raw_token}"}
    path = f"/api/v1/units/{pool.id}/scale"
    ControlPlaneService(isolated_services.context).upsert_workspace("other")

    cross_workspace = client.put(
        f"{path}?workspace=other",
        headers=headers,
        json={"desired_machines": 0},
    )
    invalid = client.put(
        path,
        headers=headers,
        json={"desired_machines": -1},
    )
    mutations.open_reservations = True
    blocked = client.put(
        path,
        headers=headers,
        json={"desired_machines": 0},
    )
    mutations.open_reservations = False
    first = client.put(
        path,
        headers=headers,
        json={"desired_machines": 0},
    )
    repeated = client.put(
        path,
        headers=headers,
        json={"desired_machines": 0},
    )
    state = client.get(f"/api/v1/units/{pool.id}/state", headers=headers)
    cross_workspace_state = client.get(
        f"/api/v1/units/{pool.id}/state?workspace=other",
        headers=headers,
    )

    assert cross_workspace.status_code == 404
    assert invalid.status_code == 422
    assert blocked.status_code == 409
    assert blocked.json() == {
        "detail": f"compute pool {pool.name!r} has active capacity reservations",
        "code": "conflict",
    }
    assert first.status_code == 200, first.text
    assert repeated.status_code == 200, repeated.text
    assert state.status_code == 200, state.text
    assert cross_workspace_state.status_code == 404
    expected = UnitScaleResponse(
        name=pool.name,
        desired_machines=0,
        max_machines=10,
        observed_machines=0,
        phase=ComputeUnitPhase.Ready,
        status=ComputeUnitPhase.Ready.value,
    )
    assert UnitScaleResponse.model_validate_json(first.content) == expected
    assert UnitScaleResponse.model_validate_json(repeated.content) == expected
    assert UnitScaleResponse.model_validate_json(state.content) == expected
    assert provider.capacity_calls == [(0, 10)]
    with isolated_services.context.database.session() as session:
        stored = ComputeUnitRepository(session).get_by_name(workspace_id, pool.name)
    assert stored is not None
    assert stored.desired_machines == 0
    assert stored.observed_machines == 0


def _seed_connection(services: ApiServices) -> str:
    with services.context.database.session() as session:
        workspace_id = services.context.default_workspace_id(session)
        now = datetime.now(UTC)
        account_id = "123456789012"
        AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=_CONNECTION_ID,
                workspace_id=workspace_id,
                account_id=account_id,
                external_id="x" * 48,
                phase=AwsAccountConnectionPhase.Ready,
                active_authorization=AwsAccountAuthorizationGeneration(
                    id=str(uuid4()),
                    generation=1,
                    role_arn=f"arn:aws:iam::{account_id}:role/compute-control",
                    authorization_mode=AwsAccountAuthorizationMode.ExistingRole,
                    phase=AwsAccountAuthorizationPhase.Ready,
                    last_validated_at=now,
                    created_at=now,
                    updated_at=now,
                ),
                node_role_arn=f"arn:aws:iam::{account_id}:role/compute-node",
                node_instance_profile_arn=(
                    f"arn:aws:iam::{account_id}:instance-profile/compute-node"
                ),
                created_at=now,
                updated_at=now,
            )
        )
    return workspace_id


class _Bootstrap:
    """A pool bootstrap provisioner with no tailnet behind it."""

    def __init__(self) -> None:
        self.released: list[str] = []

    def bootstrap(
        self,
        pool: ComputeUnitRecord,
        offer: ComputeOffer,
    ) -> ProviderUnitBootstrap:
        del offer
        return ProviderUnitBootstrap(
            control_plane_url="https://control.example.com",
            enrollment_request_id=pool.id,
            agent_version="0.1.0",
            agent_sha256="a" * 64,
            agent_binary_url=(
                f"https://s3.us-east-1.amazonaws.com/releases/agents/0.1.0/{'a' * 64}/"
                "lazycloud-agent-linux-amd64"
            ),
            worker_image_digest=f"registry.example.com/worker@sha256:{'b' * 64}",
        )

    def release(self, pool: ComputeUnitRecord) -> None:
        self.released.append(pool.id)


_bootstrap = _Bootstrap()

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

from database.repositories.billing_ledger import ContainerBillingShapeRepository
from database.repositories.compute import (
    AwsAccountConnectionRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
    ComputeUnitRepository,
)
from database.repositories.identity import UserRepository, WorkspaceRepository
from database.repositories.orchestration import MachineRepository
from shared.aws_connections import AwsAccountConnection, AwsAccountConnectionPhase
from shared.billing_quotes import ContainerShape
from shared.capacity import CapacityOwnerKind, CapacityOwnerSource
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputeCapacityMode,
    ComputeUnitRecord,
    ComputeUnitVisibility,
    MachinePool,
    UnitName,
)
from shared.placement import placement_rate_class
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner

from database import DatabaseClient


def _platform_unit(workspace_id: str, provider: str) -> ComputeUnitRecord:
    unit_id = str(uuid4())
    return ComputeUnitRecord(
        id=unit_id,
        workspace_id=workspace_id,
        name=UnitName(unit_id),
        pool=MachinePool("lazycloud"),
        provider=provider,
        provider_ref=f"{provider}:fleet",
        platform_fleet=True,
        capacity_mode=ComputeCapacityMode.Pooled,
        visibility=ComputeUnitVisibility.Internal,
        capacity_owner_id=unit_id,
        capacity_owner_kind=CapacityOwnerKind.PooledProvider,
        capacity_owner_source=CapacityOwnerSource.Provider,
        region="us-east-1",
        offer_id=unit_id,
        capability_key=unit_id,
        max_machines=100,
    )


def test_fleet_capacity_counts_commitments_and_retiring_nodes_once(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name="platform")
        customer = WorkspaceRepository(session).create(name="customer")
        repository = ComputeUnitRepository(session)
        old_machine_id = str(uuid4())
        MachineRepository(session).upsert(
            Machine(id=old_machine_id, provider="aws"), workspace_id=workspace.id
        )
        reserved = repository.upsert(
            _platform_unit(workspace.id, "aws").model_copy(
                update={
                    "desired_machines": 2,
                    "observed_machines": 2,
                    "replacement_machine_id": old_machine_id,
                    "worker_rollout_surge": True,
                    "worker_preemptible": True,
                }
            )
        )
        draining = repository.upsert(
            _platform_unit(workspace.id, "removed-provider").model_copy(
                update={"desired_machines": 1}
            )
        )
        observed = repository.upsert(
            _platform_unit(workspace.id, "hetzner").model_copy(update={"observed_machines": 4})
        )
        gpu = repository.upsert(
            _platform_unit(workspace.id, "aws").model_copy(
                update={"desired_machines": 2, "worker_gpu_type": "a100", "worker_gpu_count": 8}
            )
        )
        now = utc_now()
        connection = AwsAccountConnectionRepository(session).create(
            AwsAccountConnection(
                id=str(uuid4()),
                user_id=UserRepository(session).create(display_name="customer").id,
                account_id="123456789012",
                external_id=uuid4().hex,
                phase=AwsAccountConnectionPhase.AwaitingAuthorization,
                created_at=now,
                updated_at=now,
            )
        )
        repository.upsert(
            _platform_unit(customer.id, "aws").model_copy(
                update={
                    "platform_fleet": False,
                    "provider_connection_id": connection.id,
                    "desired_machines": 90,
                }
            )
        )
        repository.upsert(
            ComputeUnitRecord(
                id=str(uuid4()),
                workspace_id=workspace.id,
                name=UnitName("public"),
                pool=MachinePool("lazycloud"),
                desired_machines=90,
                max_machines=90,
            )
        )
        instances = ComputeProviderInstanceRepository(session)
        for unit, statuses in (
            (reserved, ("active", "terminating")),
            (draining, ("terminating", "terminating", "deleted", "failed")),
        ):
            for status in statuses:
                instances.upsert(
                    ComputeProviderInstanceRecord(
                        id=str(uuid4()),
                        pool_id=unit.id,
                        provider=unit.provider,
                        offer_id=unit.offer_id,
                        instance_id=str(uuid4()),
                        machine_id=(
                            old_machine_id
                            if unit.id == reserved.id and status == "terminating"
                            else None
                        ),
                        status=status,
                        source="pooled",
                    )
                )
        assert repository.platform_capacity_usage(gpu=False) == 10
        assert repository.platform_capacity_usage(gpu=False, excluding_unit_id=reserved.id) == 7
        assert repository.platform_capacity_usage(gpu=True) == 2
        assert {unit.id for unit in repository.list_platform_internal()} == {
            reserved.id,
            draining.id,
            observed.id,
            gpu.id,
        }
        assert [
            unit.id for unit in repository.list_platform_internal(preemptible=True, gpu=False)
        ] == [reserved.id]


def test_fleet_capacity_lock_serializes_purchases_across_workspaces(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        units = [
            _platform_unit(WorkspaceRepository(session).create(name=provider).id, provider)
            for provider in ("aws", "hetzner")
        ]
    ready = Barrier(2)

    def purchase(unit: ComputeUnitRecord) -> bool:
        with database.session() as session:
            repository = ComputeUnitRepository(session)
            ready.wait(timeout=5)
            repository.lock_platform_capacity()
            if repository.platform_capacity_usage(gpu=False) >= 1:
                return False
            repository.upsert(unit.model_copy(update={"desired_machines": 1}))
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(purchase, units)) == [False, True]
    with database.session() as session:
        assert ComputeUnitRepository(session).platform_capacity_usage(gpu=False) == 1


def test_warm_demand_uses_requested_market_and_excludes_pinned_and_customer_work(
    database: DatabaseClient,
) -> None:
    with database.session() as session:
        workspace = WorkspaceRepository(session).create(name="arrivals")
        shapes = ContainerBillingShapeRepository(session)
        for index, (owner, pinned, preemptible, gpu_count) in enumerate(
            (
                (UsageBillingOwner.PlatformFleet, False, True, 0),
                (UsageBillingOwner.PlatformFleet, False, False, 0),
                (UsageBillingOwner.PlatformFleet, True, True, 0),
                (UsageBillingOwner.ConnectedCloud, False, True, 0),
                (UsageBillingOwner.PlatformFleet, False, True, 1),
            ),
            start=1,
        ):
            shapes.record(
                container_id=str(uuid4()),
                workspace_id=workspace.id,
                shape=ContainerShape(
                    owner,
                    "a100" if gpu_count else "",
                    index * 1000,
                    index * 1024,
                    gpu_count,
                    placement_rate_class(pinned=pinned, preemptible=preemptible),
                ),
            )
        repository = ComputeUnitRepository(session)
        since = utc_now() - timedelta(minutes=1)
        assert [
            arrival.cpu_millicores
            for arrival in repository.recent_platform_cpu_arrivals(since, preemptible=True)
        ] == [1000]
        assert [
            arrival.cpu_millicores
            for arrival in repository.recent_platform_cpu_arrivals(since, preemptible=False)
        ] == [2000]

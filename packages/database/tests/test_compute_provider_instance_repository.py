from __future__ import annotations

from uuid import uuid4

from api.server.services import ApiServices
from database.repositories.compute import (
    ComputePoolRepository,
    ComputeProviderInstanceRecord,
    ComputeProviderInstanceRepository,
)
from database.repositories.orchestration import MachineRepository
from shared.compute_fleet import Machine
from shared.compute_policy import (
    ComputePoolRecord,
    MachinePool,
    UnitName,
)


def test_provider_instance_machine_binding_is_idempotent_and_fenced(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        pool = ComputePoolRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=UnitName("provider-binding"),
            machine_pool=MachinePool("provider-binding"),
        )
        ComputePoolRepository(session).upsert(pool)
        instance = ComputeProviderInstanceRecord(
            id=str(uuid4()),
            provider="aws",
            offer_id="i4i.xlarge:us-east-1",
            instance_type="i4i.xlarge",
            instance_id="i-0123456789abcdef0",
            status="running",
            source="pooled",
            pool_id=pool.id,
        )
        repository = ComputeProviderInstanceRepository(session)
        repository.upsert(instance)

        machine_id = str(uuid4())
        MachineRepository(session).upsert(
            Machine(id=machine_id, pool=pool.name, provider="aws"),
            workspace_id=workspace_id,
        )

        bound = repository.bind_machine(pool.id, instance.instance_id or "", machine_id)
        assert bound is not None
        assert bound.machine_id == machine_id
        assert repository.bind_machine(pool.id, instance.instance_id or "", machine_id) == bound
        assert repository.bind_machine(pool.id, instance.instance_id or "", str(uuid4())) is None
        assert repository.bind_machine(pool.id, "i-0ffffffffffffffff", machine_id) is None


def test_unbinding_releases_only_the_machine_it_names(
    isolated_services: ApiServices,
) -> None:
    """A machine torn down after binding must leave no reference behind.

    The reference outlives the machine row otherwise, and every later pool sync
    fails its foreign key — which takes enrollment down for the whole pool.
    """
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        pool = ComputePoolRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name=UnitName("provider-unbinding"),
            machine_pool=MachinePool("provider-unbinding"),
        )
        ComputePoolRepository(session).upsert(pool)
        instance = ComputeProviderInstanceRecord(
            id=str(uuid4()),
            provider="aws",
            offer_id="i4i.xlarge:us-east-1",
            instance_type="i4i.xlarge",
            instance_id="i-0abcdef0123456789",
            status="running",
            source="pooled",
            pool_id=pool.id,
        )
        repository = ComputeProviderInstanceRepository(session)
        repository.upsert(instance)

        machines = MachineRepository(session)
        first = str(uuid4())
        machines.upsert(
            Machine(id=first, pool=pool.name, provider="aws"), workspace_id=workspace_id
        )
        assert repository.bind_machine(pool.id, instance.instance_id or "", first) is not None

        # A stale release must not strand the binding a later enrollment made.
        second = str(uuid4())
        machines.upsert(
            Machine(id=second, pool=pool.name, provider="aws"), workspace_id=workspace_id
        )
        kept = repository.unbind_machine(pool.id, instance.instance_id or "", second)
        assert kept is not None
        assert kept.machine_id == first

        released = repository.unbind_machine(pool.id, instance.instance_id or "", first)
        assert released is not None
        assert released.machine_id is None
        # Released rows rebind cleanly rather than staying poisoned.
        assert repository.bind_machine(pool.id, instance.instance_id or "", second) is not None

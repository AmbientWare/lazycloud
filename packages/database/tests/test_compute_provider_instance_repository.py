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
from shared.compute_policy import ComputePoolRecord


def test_provider_instance_machine_binding_is_idempotent_and_fenced(
    isolated_services: ApiServices,
) -> None:
    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        pool = ComputePoolRecord(
            id=str(uuid4()),
            workspace_id=workspace_id,
            name="provider-binding",
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

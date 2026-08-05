from __future__ import annotations

import pytest
from api.server.services import ApiServices
from database.repositories.compute import ComputePoolRepository
from shared.errors import ConflictError


def test_pool_repository_preserves_owner_on_policy_update_and_rejects_replacement(
    isolated_services: ApiServices,
) -> None:
    created = isolated_services.compute.create_pool("private", provider="agent")
    updated = isolated_services.compute.create_pool(
        "private",
        provider="agent",
        max_machines=3,
        worker_cpu_millicores=2_000,
        worker_memory_mib=4_096,
    )

    assert updated.capacity_owner_id == created.capacity_owner_id
    assert updated.max_machines == 3

    with isolated_services.context.database.session() as session:
        repository = ComputePoolRepository(session)
        with pytest.raises(ConflictError, match="capacity owner is immutable"):
            repository.upsert(
                updated.model_copy(
                    update={
                        "capacity_owner_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    }
                )
            )

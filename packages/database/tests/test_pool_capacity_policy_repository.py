from __future__ import annotations

import pytest
from api.server.services import ApiServices
from database.repositories.orchestration import PoolRepository
from shared.errors import ConflictError


def test_pool_repository_preserves_owner_on_policy_update_and_rejects_replacement(
    isolated_services: ApiServices,
) -> None:
    created = isolated_services.compute.create_pool("private", provider="agent")
    updated = isolated_services.compute.create_pool(
        "private",
        provider="agent",
        max_workers=3,
        worker_cpu_millicores=2_000,
        worker_memory_mib=4_096,
    )

    assert updated.capacity_owner_id == created.capacity_owner_id
    assert updated.max_workers == 3

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.default_workspace_id(session)
        repository = PoolRepository(session)
        with pytest.raises(ConflictError, match="capacity owner is immutable"):
            repository.upsert(
                updated.model_copy(
                    update={
                        "capacity_owner_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    }
                ),
                workspace_id=workspace_id,
            )

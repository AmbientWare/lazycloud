from __future__ import annotations

import pytest
from api.control_runtime import _reconcile_bootstrap_capacity
from api.server.services import ApiServices
from api.settings import CapacityBootstrapPool, CapacityBootstrapSettings
from database.repositories.orchestration import PoolRepository
from shared.errors import ConflictError


def test_capacity_bootstrap_reconciles_policy_with_an_immutable_owner(
    isolated_services: ApiServices,
) -> None:
    owner_id = "839fc92e-c26d-4e31-84c1-a827c2768607"
    initial = CapacityBootstrapSettings(
        pools=(
            CapacityBootstrapPool(
                name="compose-cpu",
                capacity_owner_id=owner_id,
                initial_workers=1,
                min_workers=1,
                max_workers=1,
                default_eligible=True,
                worker_cpu_millicores=4_000,
                worker_memory_mib=8_192,
                worker_runtimes=("runc", "runsc"),
            ),
        )
    )

    _reconcile_bootstrap_capacity(isolated_services, initial)
    _reconcile_bootstrap_capacity(
        isolated_services,
        CapacityBootstrapSettings(
            pools=(
                initial.pools[0].model_copy(
                    update={
                        "max_workers": 2,
                        "min_workers": 0,
                        "initial_workers": 0,
                    }
                ),
            )
        ),
    )

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.workspace(session, "default").id
        pool = PoolRepository(session).get("compose-cpu", workspace_id=workspace_id)
    assert pool is not None
    assert pool.capacity_owner_id == owner_id
    assert pool.max_workers == 2
    assert pool.worker_runtimes == ("runc", "runsc")

    with pytest.raises(ConflictError, match="capacity owner is immutable"):
        _reconcile_bootstrap_capacity(
            isolated_services,
            CapacityBootstrapSettings(
                pools=(
                    initial.pools[0].model_copy(
                        update={"capacity_owner_id": "f82b1a1d-070e-4fa8-8bad-f2af464c0fb9"}
                    ),
                )
            ),
        )


def test_capacity_bootstrap_rejects_duplicate_pool_or_owner_identity() -> None:
    first = CapacityBootstrapPool(
        name="cpu",
        capacity_owner_id="839fc92e-c26d-4e31-84c1-a827c2768607",
    )
    with pytest.raises(ValueError, match="workspace/name"):
        CapacityBootstrapSettings(pools=(first, first))
    with pytest.raises(ValueError, match="capacity owner ids"):
        CapacityBootstrapSettings(pools=(first, first.model_copy(update={"name": "gpu"})))

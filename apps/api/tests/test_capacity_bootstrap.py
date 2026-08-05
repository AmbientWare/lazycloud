from __future__ import annotations

import pytest
from api.control_runtime import _reconcile_bootstrap_capacity
from api.server.services import ApiServices
from api.settings import CapacityBootstrapSettings, CapacityBootstrapUnit
from database.repositories.compute import ComputeUnitRepository
from shared.errors import ConflictError


def test_capacity_bootstrap_reconciles_policy_with_an_immutable_owner(
    isolated_services: ApiServices,
) -> None:
    owner_id = "839fc92e-c26d-4e31-84c1-a827c2768607"
    initial = CapacityBootstrapSettings(
        units=(
            CapacityBootstrapUnit(
                name="compose-cpu",
                capacity_owner_id=owner_id,
                pool="lazycloud",
                initial_machines=1,
                min_machines=1,
                max_machines=1,
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
            units=(
                initial.units[0].model_copy(
                    update={
                        "max_machines": 2,
                        "min_machines": 0,
                        "initial_machines": 0,
                    }
                ),
            )
        ),
    )

    with isolated_services.context.database.session() as session:
        workspace_id = isolated_services.context.workspace(session, "default").id
        unit = ComputeUnitRepository(session).get_by_name(workspace_id, "compose-cpu")
    assert unit is not None
    assert unit.capacity_owner_id == owner_id
    assert unit.pool == "lazycloud"
    assert unit.max_machines == 2
    assert unit.worker_runtimes == ("runc", "runsc")

    with pytest.raises(ConflictError, match="capacity owner is immutable"):
        _reconcile_bootstrap_capacity(
            isolated_services,
            CapacityBootstrapSettings(
                units=(
                    initial.units[0].model_copy(
                        update={"capacity_owner_id": "f82b1a1d-070e-4fa8-8bad-f2af464c0fb9"}
                    ),
                )
            ),
        )


def test_capacity_bootstrap_rejects_duplicate_pool_or_owner_identity() -> None:
    first = CapacityBootstrapUnit(
        name="cpu",
        capacity_owner_id="839fc92e-c26d-4e31-84c1-a827c2768607",
    )
    with pytest.raises(ValueError, match="workspace/name"):
        CapacityBootstrapSettings(units=(first, first))
    with pytest.raises(ValueError, match="capacity owner ids"):
        CapacityBootstrapSettings(units=(first, first.model_copy(update={"name": "gpu"})))

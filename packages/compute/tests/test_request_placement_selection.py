from __future__ import annotations

import pytest
from compute.policy import WorkspaceComputePolicyService
from compute.providers import ResolvedComputeProvider
from compute.request_placement import (
    ComputeCapacityPlacementRequest,
    ComputeCapacityPlacementService,
)
from compute.service import ComputeService
from database.context import ServiceContext
from shared.compute_policy import (
    LAZYCLOUD_MACHINE_POOL,
    ComputeResourceRequirements,
    MachinePool,
    UnitName,
)
from shared.errors import UpstreamUnavailableError


def _workspace_id(context: ServiceContext) -> str:
    with context.database.session() as session:
        return context.default_workspace_id(session)


def test_placement_names_the_pool_and_leaves_the_unit_to_arbitration(
    service_context: ServiceContext,
) -> None:
    """Placement resolves a pool; it never picks which unit inside it serves.

    Two units feed one pool here. Placement answering with the pool is what
    leaves the acquisition loop both candidates to fail over between; answering
    with a unit would pin the request to one of them.
    """
    workspace_id = _workspace_id(service_context)
    for name, owner in (
        ("unit-a", "10000000-0000-4000-8000-000000000001"),
        ("unit-b", "20000000-0000-4000-8000-000000000002"),
    ):
        ComputeService(service_context).create_unit(
            UnitName(name),
            workspace=workspace_id,
            pool=MachinePool("shared-pool"),
            provider="agent",
            capacity_owner_id=owner,
            worker_cpu_millicores=4_000,
            worker_memory_mib=8_192,
        )
    placement = ComputeCapacityPlacementService(
        service_context,
        WorkspaceComputePolicyService(service_context),
        ComputeService(service_context),
    )

    result = placement.place(
        ComputeCapacityPlacementRequest(
            workspace_id=workspace_id,
            requested_pool="shared-pool",
            requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
        )
    )

    assert result.pool == "shared-pool"


def test_pool_selection_survives_supplier_failure_until_capacity_is_needed(
    service_context: ServiceContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable_providers(
        self: ComputeService, workspace_id: str
    ) -> tuple[ResolvedComputeProvider, ...]:
        raise UpstreamUnavailableError("supplier unavailable")

    monkeypatch.setattr(ComputeService, "pooled_providers", unavailable_providers)
    placement = ComputeCapacityPlacementService(
        service_context,
        WorkspaceComputePolicyService(service_context),
        ComputeService(service_context),
    )
    request = ComputeCapacityPlacementRequest(
        workspace_id=_workspace_id(service_context),
        requirements=ComputeResourceRequirements(cpu_millicores=1_000, memory_mb=1_024),
    )

    assert placement.place(request).pool == LAZYCLOUD_MACHINE_POOL
    with pytest.raises(UpstreamUnavailableError, match="supplier unavailable"):
        placement.purchase_candidates(request)

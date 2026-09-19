from __future__ import annotations

from control.placement import PlacementResolver
from database.records.apps import StubRecord
from database.repositories.apps import DeploymentRepository
from database.repositories.identity import WorkspaceRecord
from database.types import DatabaseSession
from shared.errors import NotFoundError
from shared.placement import Placement


def workload_placement(
    session: DatabaseSession,
    *,
    resolver: PlacementResolver,
    stub: StubRecord,
    workspace: WorkspaceRecord,
) -> Placement:
    """Where one container requested for `stub` runs.

    A deployed stub runs where its deployment was pinned at deploy time; that pin
    never moves, so a machine that has since left keeps the deployment waiting
    rather than sending it somewhere else. Everything else resolves the
    workspace's location and the machine the stub names at request time, because
    a stub is reused across runs and can outlive the machine it first ran on. A
    named machine that has left refuses the request here, naming the machine.
    """
    if stub.deployment_id:
        deployment = DeploymentRepository(session).get(
            stub.deployment_id, workspace_id=stub.workspace_id, include_deleted=True
        )
        if deployment is None:
            raise NotFoundError(f"deployment not found for stub: {stub.deployment_id}")
        return deployment.placement
    return resolver.resolve_placement(session, workspace, stub.config.machine)


__all__ = ["workload_placement"]

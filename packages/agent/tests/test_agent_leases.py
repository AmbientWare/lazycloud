import pytest
from agent.service import AgentService
from database.context import ServiceContext
from database.repositories.identity import WorkspaceRepository
from shared.compute_fleet import LeaseStatus


def test_agent_lease_scope_and_agent_deletion(service_context: ServiceContext) -> None:
    service = AgentService(service_context)
    with service_context.database.session() as session:
        other = WorkspaceRepository(session).create(name="other-agent-owner")
    agent = service.register("lease-owner")
    lease = service.lease(agent.id, resource_type="task", resource_id="task-a")
    assert service.list_leases(workspace=other.id) == []
    with pytest.raises(KeyError, match="lease not found"):
        service.release(lease.id, workspace=other.id)
    [active] = service.list_leases()
    assert active.id == lease.id and active.status is LeaseStatus.Active
    service.release(lease.id)
    assert service.list_leases() == []
    assert service.list_leases(include_inactive=True)[0].status is LeaseStatus.Released
    service.delete(agent.id)
    assert service.list_leases(include_inactive=True) == []

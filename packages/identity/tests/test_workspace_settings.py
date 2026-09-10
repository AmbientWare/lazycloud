from __future__ import annotations

from database.context import ServiceContext
from identity.auth import AuthService
from identity.workspaces import WorkspaceSettingsService


def test_workspace_audit_cursor_round_trips_through_validated_contract(
    service_context: ServiceContext,
) -> None:
    _raw_token, actor = AuthService(service_context).create_token("audit-actor")
    service = WorkspaceSettingsService(service_context)
    service.rename("default", name="workspace-alpha", actor=actor)
    service.rename("workspace-alpha", name="workspace-beta", actor=actor)

    first = service.audit_history("workspace-beta", limit=1)

    assert len(first.page.records) == 1
    assert first.next
    second = service.audit_history("workspace-beta", limit=1, cursor=first.next)
    assert len(second.page.records) == 1
    assert second.page.records[0].id != first.page.records[0].id

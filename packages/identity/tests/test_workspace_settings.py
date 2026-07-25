from __future__ import annotations

from api.server.services import ApiServices
from identity.auth import AuthService
from identity.workspaces import WorkspaceSettingsService


def test_workspace_audit_cursor_round_trips_through_validated_contract(
    isolated_services: ApiServices,
) -> None:
    _raw_token, actor = AuthService(isolated_services.context).create_token("audit-actor")
    service = WorkspaceSettingsService(isolated_services.context)
    service.rename("default", name="workspace-alpha", actor=actor)
    service.rename("workspace-alpha", name="workspace-beta", actor=actor)

    first = service.audit_history("workspace-beta", limit=1)

    assert len(first.page.records) == 1
    assert first.next
    second = service.audit_history("workspace-beta", limit=1, cursor=first.next)
    assert len(second.page.records) == 1
    assert second.page.records[0].id != first.page.records[0].id

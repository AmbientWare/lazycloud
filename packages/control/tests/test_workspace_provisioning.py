from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from control.service import ControlServices
from database.context import ServiceContext
from database.repositories.identity import WorkspaceMemberRepository
from identity.users import UserService
from storage.workspace_provisioning import WorkspaceStorageError
from tests.fakes import FakeWorkspaceBuckets


class UnavailableBuckets(FakeWorkspaceBuckets):
    def validate_bucket_access(self, bucket: str | None = None) -> None:
        raise OSError("storage unavailable")


def test_failed_provisioning_is_repaired_by_concurrent_sign_ins(
    committed_service_context: ServiceContext,
) -> None:
    control = ControlServices.create(
        committed_service_context, workspace_storage_client=UnavailableBuckets()
    )
    user = UserService(committed_service_context).create(display_name="new-owner")
    with pytest.raises(WorkspaceStorageError):
        control.workspaces.ensure_default_workspace(user.id, "owner")
    control.workspaces.storage.client = FakeWorkspaceBuckets()
    start = Barrier(4)

    def provision(_index: int) -> tuple[str, str | None]:
        start.wait(timeout=10)
        workspace = control.workspaces.ensure_default_workspace(user.id, "owner")
        assert workspace.storage.bucket
        return workspace.id, workspace.primary_token_id

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = set(executor.map(provision, range(4)))
    assert len(results) == 1
    workspace_id, primary_token_id = results.pop()
    assert primary_token_id
    with committed_service_context.database.session() as session:
        assert WorkspaceMemberRepository(session).owned_workspace_ids(user.id) == [workspace_id]


def test_concurrent_owners_get_distinct_default_workspaces_with_the_same_preferred_name(
    committed_service_context: ServiceContext,
) -> None:
    control = ControlServices.create(
        committed_service_context, workspace_storage_client=FakeWorkspaceBuckets()
    )
    users = [
        UserService(committed_service_context).create(display_name=f"owner-{index}").id
        for index in range(2)
    ]
    start = Barrier(2)

    def provision(user_id: str) -> str:
        start.wait(timeout=10)
        return control.workspaces.ensure_default_workspace(user_id, "same-name").id

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert len(set(executor.map(provision, users))) == 2

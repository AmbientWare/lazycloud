from __future__ import annotations

from dataclasses import dataclass

from billing.admission import DatabaseBillingAdmission
from observability.workspace_changes import WorkspaceChangePublisher
from shared.workspace_storage import ConnectedWorkspaceStorageIssuer
from storage.service import ObjectStorage
from storage.workspace_provisioning import WorkspaceBucketClient, WorkspaceBucketProvisioner

from control.cloning import StubCloneService
from control.concurrency import ConcurrencyService
from control.context import ControlContext
from control.sandboxes import SandboxQueryService
from control.stubs import StubService
from control.workspaces import WorkspaceCreationAdmission, WorkspaceService


@dataclass(frozen=True, slots=True)
class ControlServices:
    context: ControlContext
    workspaces: WorkspaceService
    stubs: StubService
    cloning: StubCloneService
    concurrency: ConcurrencyService
    sandboxes: SandboxQueryService

    @classmethod
    def create(
        cls,
        context: ControlContext,
        *,
        workspace_storage_client: WorkspaceBucketClient | None = None,
        public_http_origin: str = "",
        connected_workspace_storage: ConnectedWorkspaceStorageIssuer | None = None,
        workspace_changes: WorkspaceChangePublisher | None = None,
        workspace_admission: WorkspaceCreationAdmission | None = None,
        object_storage: ObjectStorage | None = None,
    ) -> ControlServices:
        workspaces = WorkspaceService(
            context,
            storage=WorkspaceBucketProvisioner(
                context.database,
                workspace_storage_client,
                public_http_origin,
                connected_workspace_storage,
            ),
            workspace_admission=workspace_admission or DatabaseBillingAdmission(),
        )
        stubs = StubService(context, workspace_changes)
        return cls(
            context,
            workspaces,
            stubs,
            StubCloneService(context, stubs, object_storage, workspace_changes),
            ConcurrencyService(context, workspace_changes),
            SandboxQueryService(context, stubs),
        )

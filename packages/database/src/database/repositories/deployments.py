from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from database.mappers.apps import (
    deployment_from_table,
    write_deployment_row,
)
from database.repositories.apps import AppRepository
from database.repositories.identity import WorkspaceRepository
from database.tables.apps import (
    AppTable,
    DeploymentTable,
    StubTable,
)
from database.tables.orchestration import ContainerTable
from shared.containers import LIVE_CONTAINER_STATUSES
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind, PodRole
from shared.disks import DISK_ROOT_MOUNT_PATH
from shared.errors import ConflictError
from shared.placement import Placement
from sqlalchemy import (
    and_,
    exists,
    func,
    literal,
    select,
    tuple_,
    update,
)
from sqlalchemy.orm import Session

_ROOT_DISK_PATH = f'$[*] ? (!exists(@.mount_path) || @.mount_path == "{DISK_ROOT_MOUNT_PATH}")'


@dataclass(frozen=True, slots=True)
class SshPodRow:
    app_id: str
    app_name: str
    pod: str
    role: PodRole
    deployment_id: str
    active: bool
    ssh: bool


@dataclass(slots=True)
class DeploymentRepository:
    session: Session

    def resolve(self, identifier: str, *, workspace_id: str) -> Deployment | None:
        try:
            deployment_id = str(UUID(identifier))
        except ValueError:
            deployment_id = None
        if deployment_id is not None:
            found = self.get(deployment_id, workspace_id=workspace_id)
            if found is not None:
                return found
        return self.latest_by_name(identifier, workspace_id=workspace_id)

    def older_active_ids(self, deployment: Deployment, *, workspace_id: str) -> list[str]:
        return list(
            self.session.scalars(
                select(DeploymentTable.id).where(
                    DeploymentTable.workspace_id == workspace_id,
                    DeploymentTable.app_id == deployment.app_id,
                    DeploymentTable.name == deployment.name,
                    DeploymentTable.kind == deployment.kind.value,
                    DeploymentTable.version < deployment.version,
                    DeploymentTable.deleted_at.is_(None),
                    DeploymentTable.active.is_(True),
                )
            )
        )

    def latest_by_name(
        self, name: str, *, workspace_id: str | None, active: bool | None = None
    ) -> Deployment | None:
        statement = select(DeploymentTable).where(
            DeploymentTable.name == name, DeploymentTable.deleted_at.is_(None)
        )
        if workspace_id is not None:
            statement = statement.where(DeploymentTable.workspace_id == workspace_id)
        if active is not None:
            statement = statement.where(DeploymentTable.active.is_(active))
        row = self.session.scalars(
            statement.order_by(DeploymentTable.version.desc()).limit(1)
        ).first()
        return deployment_from_table(row) if row is not None else None

    def name_live_in_other_app(
        self, name: str, *, kind: DeploymentKind, app_id: str, workspace_id: str
    ) -> bool:
        """Whether an active deployment of this kind and name lives in another app."""
        return bool(
            self.session.scalar(
                select(
                    exists().where(
                        DeploymentTable.workspace_id == workspace_id,
                        DeploymentTable.kind == kind.value,
                        DeploymentTable.name == name,
                        DeploymentTable.app_id != app_id,
                        DeploymentTable.active.is_(True),
                        DeploymentTable.deleted_at.is_(None),
                    )
                )
            )
        )

    def lock_live(self, deployment_id: str, *, workspace_id: str) -> bool:
        return (
            self.session.scalar(
                select(DeploymentTable.id)
                .where(
                    DeploymentTable.id == deployment_id,
                    DeploymentTable.workspace_id == workspace_id,
                    DeploymentTable.deleted_at.is_(None),
                )
                .with_for_update(read=True)
            )
            is not None
        )

    def lock_stub_deployment_active(self, stub_id: str, *, workspace_id: str) -> bool | None:
        return self.session.scalar(
            select(and_(DeploymentTable.active.is_(True), DeploymentTable.deleted_at.is_(None)))
            .join(StubTable, StubTable.deployment_id == DeploymentTable.id)
            .where(
                StubTable.id == stub_id,
                StubTable.workspace_id == workspace_id,
                DeploymentTable.workspace_id == workspace_id,
            )
            .with_for_update(read=True, of=DeploymentTable)
        )

    def lock_invocation_active(
        self, deployment_id: str, *, workspace_id: str, stub_id: str
    ) -> bool | None:
        return self.session.scalar(
            select(DeploymentTable.active)
            .where(
                DeploymentTable.id == deployment_id,
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.stub_id == stub_id,
                DeploymentTable.deleted_at.is_(None),
            )
            .with_for_update(read=True)
        )

    def workspaces_pinned_to(
        self, placement: Placement, workspace_ids: Collection[str]
    ) -> list[str]:
        """Workspaces among `workspace_ids` with a live deployment pinned to `placement`.

        Only deployments count: they keep the placement they were deployed with,
        while a run or sandbox resolves its placement each time it starts.
        """
        if not workspace_ids:
            return []
        return list(
            self.session.scalars(
                select(DeploymentTable.workspace_id)
                .where(
                    DeploymentTable.placement == placement.key,
                    DeploymentTable.workspace_id.in_(list(workspace_ids)),
                    DeploymentTable.deleted_at.is_(None),
                )
                .distinct()
            )
        )

    def get_for_update(self, deployment_id: str, *, workspace_id: str) -> Deployment | None:
        row = self.session.scalars(
            select(DeploymentTable)
            .where(
                DeploymentTable.id == deployment_id,
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.deleted_at.is_(None),
            )
            .with_for_update()
        ).first()
        return deployment_from_table(row) if row is not None else None

    def upsert(self, deployment: Deployment, *, workspace_id: str) -> Deployment:
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        if deployment.app_id is not None:
            app = AppRepository(self.session).get_for_update(
                deployment.app_id, workspace_id=workspace_id, include_deleted=True
            )
            if app is None or (app.deleted_at is not None and deployment.deleted_at is None):
                raise ConflictError("deployment app is unavailable")
        row = self.session.get(DeploymentTable, deployment.id, populate_existing=True)
        if row is None:
            row = DeploymentTable(id=deployment.id, workspace_id=workspace_id)
            self.session.add(row)
        elif str(row.workspace_id) != workspace_id:
            raise ConflictError("deployment ownership cannot change")
        elif row.deleted_at is not None and deployment.deleted_at is None:
            raise ConflictError("deleted deployment cannot be restored")
        write_deployment_row(row, deployment)
        self.session.flush()
        return deployment_from_table(row)

    def next_version(
        self, *, workspace_id: str, app_id: str | None, name: str, kind: DeploymentKind
    ) -> int:
        latest = self.session.scalar(
            select(func.max(DeploymentTable.version)).where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.app_id.is_not_distinct_from(app_id),
                DeploymentTable.name == name,
                DeploymentTable.kind == kind.value,
            )
        )
        return (latest or 0) + 1

    def newest_active_version(
        self, *, workspace_id: str, app_id: str | None, name: str, kind: DeploymentKind
    ) -> int | None:
        return self.session.scalar(
            select(func.max(DeploymentTable.version)).where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.app_id.is_not_distinct_from(app_id),
                DeploymentTable.name == name,
                DeploymentTable.kind == kind.value,
                DeploymentTable.deleted_at.is_(None),
                DeploymentTable.active.is_(True),
            )
        )

    def deactivate_superseded_versions(
        self,
        *,
        workspace_id: str,
        app_id: str | None,
        name: str,
        kind: DeploymentKind,
        now: datetime,
    ) -> list[Deployment]:
        """Switch off every active version below the newest registered active one.

        The version number decides, not the order deploys finish in: an older
        deploy that registers last is switched off by its own call. A version
        still registering does not count as newest, so a deploy that then fails
        cannot leave the workload with nothing on. Locking the workload's rows
        first makes two concurrent calls take turns.
        """
        workload = and_(
            DeploymentTable.workspace_id == workspace_id,
            DeploymentTable.app_id.is_not_distinct_from(app_id),
            DeploymentTable.name == name,
            DeploymentTable.kind == kind.value,
            DeploymentTable.deleted_at.is_(None),
        )
        self.session.execute(select(DeploymentTable.id).where(workload).with_for_update())
        newest = (
            select(func.max(DeploymentTable.version))
            .where(
                workload,
                DeploymentTable.active.is_(True),
                DeploymentTable.stub_id.is_not(None),
            )
            .scalar_subquery()
        )
        rows = self.session.scalars(
            update(DeploymentTable)
            .where(workload, DeploymentTable.active.is_(True), DeploymentTable.version < newest)
            .values(active=False, updated_at=now)
            .returning(DeploymentTable)
            .execution_options(synchronize_session=False)
        )
        return [deployment_from_table(row) for row in rows]

    def assert_subdomain_unclaimed(
        self,
        subdomain: str,
        *,
        workspace_id: str,
        app_id: str | None,
        name: str,
        kind: DeploymentKind,
    ) -> None:
        """Refuse a subdomain another resource already answers on.

        `uq_deployments_subdomain_version_active` only catches a digest collision when
        both resources reach the same version number. Two colliding resources sitting at
        different versions would otherwise share a hostname, and the edge would hand one
        tenant's traffic to the other's resource.
        """
        same_resource = and_(
            DeploymentTable.workspace_id == workspace_id,
            DeploymentTable.app_id.is_not_distinct_from(app_id),
            DeploymentTable.name == name,
            DeploymentTable.kind == kind.value,
        )
        conflict = self.session.execute(
            select(DeploymentTable.id)
            .where(DeploymentTable.subdomain == subdomain)
            .where(DeploymentTable.deleted_at.is_(None))
            .where(~same_resource)
            .limit(1)
        ).first()
        if conflict is not None:
            raise ConflictError(
                f"subdomain {subdomain} already belongs to another resource; "
                f"rename {name} to claim a different one"
            )

    def ssh_pods(
        self,
        *,
        workspace_id: str,
        app: str | None = None,
        pod: str | None = None,
        role: PodRole | None = None,
        after: tuple[str, str] | None = None,
        limit: int,
    ) -> list[SshPodRow]:
        """Pods whose newest version is active and serves SSH, ordered by app and name.

        The newest version decides, because it is the one an SSH connection
        reaches. Filtered by `pod`, stopped pods and pods without SSH come back
        too, marked as such, so a caller can say why it cannot connect.
        """
        newest = (
            select(
                AppTable.id.label("app_id"),
                AppTable.name.label("app_name"),
                DeploymentTable.name.label("pod"),
                DeploymentTable.id.label("deployment_id"),
                DeploymentTable.active.label("active"),
                StubTable.ssh.label("ssh"),
                StubTable.role.label("role"),
            )
            .join(AppTable, AppTable.id == DeploymentTable.app_id)
            .join(StubTable, StubTable.id == DeploymentTable.stub_id)
            .where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.kind == DeploymentKind.Pod.value,
                DeploymentTable.deleted_at.is_(None),
                AppTable.deleted_at.is_(None),
            )
            .distinct(AppTable.name, DeploymentTable.name)
            .order_by(AppTable.name, DeploymentTable.name, DeploymentTable.version.desc())
        )
        if app is not None:
            newest = newest.where(AppTable.name == app)
        if pod is not None:
            newest = newest.where(DeploymentTable.name == pod)
        candidates = newest.subquery()
        statement = select(
            candidates.c.app_id,
            candidates.c.app_name,
            candidates.c.pod,
            candidates.c.role,
            candidates.c.deployment_id,
            candidates.c.active,
            candidates.c.ssh,
        )
        if pod is None:
            statement = statement.where(candidates.c.ssh.is_(True), candidates.c.active.is_(True))
        if role is not None:
            statement = statement.where(candidates.c.role == role.value)
        if after is not None:
            statement = statement.where(
                tuple_(candidates.c.app_name, candidates.c.pod)
                > tuple_(literal(after[0]), literal(after[1]))
            )
        rows = self.session.execute(
            statement.order_by(candidates.c.app_name, candidates.c.pod).limit(limit)
        ).tuples()
        return [
            SshPodRow(
                app_id=str(app_id),
                app_name=app_name,
                pod=pod_name,
                role=PodRole(role) if role else PodRole.Service,
                deployment_id=str(deployment_id),
                active=bool(active),
                ssh=bool(ssh),
            )
            for app_id, app_name, pod_name, role, deployment_id, active, ssh in rows
        ]

    def live_container_ids(
        self,
        *,
        workspace_id: str,
        deployment_id: str | None = None,
        workload: tuple[str | None, str, DeploymentKind] | None = None,
    ) -> list[str]:
        """Live containers of one deployment, or of every live version of a workload.

        `workload` is `(app_id, name, kind)`.
        """
        statement = (
            select(ContainerTable.id)
            .join(StubTable, StubTable.id == ContainerTable.stub_id)
            .join(DeploymentTable, DeploymentTable.id == StubTable.deployment_id)
            .where(
                ContainerTable.workspace_id == workspace_id,
                ContainerTable.status.in_([status.value for status in LIVE_CONTAINER_STATUSES]),
                DeploymentTable.workspace_id == workspace_id,
            )
        )
        if deployment_id is not None:
            statement = statement.where(DeploymentTable.id == deployment_id)
        if workload is not None:
            app_id, name, kind = workload
            statement = statement.where(
                DeploymentTable.app_id.is_not_distinct_from(app_id),
                DeploymentTable.name == name,
                DeploymentTable.kind == kind.value,
                DeploymentTable.deleted_at.is_(None),
            )
        return [str(container_id) for container_id in self.session.scalars(statement)]

    def delete_versions(
        self,
        *,
        workspace_id: str,
        app_id: str | None,
        name: str,
        kind: DeploymentKind,
        now: datetime,
    ) -> list[Deployment]:
        """Soft-delete every live version of one workload and return them.

        A stopped older version is still listed as the workload, so deleting only
        the newest leaves the workload standing.
        """
        WorkspaceRepository(self.session).lock_active_owner(workspace_id)
        rows = self.session.scalars(
            update(DeploymentTable)
            .where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.app_id.is_not_distinct_from(app_id),
                DeploymentTable.name == name,
                DeploymentTable.kind == kind.value,
                DeploymentTable.deleted_at.is_(None),
            )
            .values(active=False, deleted_at=now, updated_at=now)
            .returning(DeploymentTable)
            .execution_options(synchronize_session=False)
        )
        return [deployment_from_table(row) for row in rows]

    def deactivate_for_workspace_deletion(
        self, *, workspace_id: str, now: datetime
    ) -> list[Deployment]:
        rows = self.session.scalars(
            update(DeploymentTable)
            .where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.active.is_(True),
                DeploymentTable.deleted_at.is_(None),
            )
            .values(active=False, updated_at=now)
            .returning(DeploymentTable)
        )
        return [deployment_from_table(row) for row in rows]

    def get(
        self,
        deployment_id: str,
        *,
        workspace_id: str,
        include_deleted: bool = False,
    ) -> Deployment | None:
        try:
            UUID(deployment_id)
        except ValueError:
            return None
        statement = select(DeploymentTable).where(
            DeploymentTable.id == deployment_id,
            DeploymentTable.workspace_id == workspace_id,
        )
        if not include_deleted:
            statement = statement.where(DeploymentTable.deleted_at.is_(None))
        row = self.session.scalar(statement)
        return deployment_from_table(row) if row is not None else None

    def get_across_workspaces(
        self,
        deployment_id: str,
        *,
        include_deleted: bool = False,
    ) -> Deployment | None:
        """System lookup for scheduler/worker paths acting under their own authority."""
        try:
            UUID(deployment_id)
        except ValueError:
            return None
        statement = select(DeploymentTable).where(DeploymentTable.id == deployment_id)
        if not include_deleted:
            statement = statement.where(DeploymentTable.deleted_at.is_(None))
        row = self.session.scalar(statement)
        return deployment_from_table(row) if row is not None else None

    def workspace_id(self, deployment_id: str) -> str | None:
        value = self.session.scalar(
            select(DeploymentTable.workspace_id).where(DeploymentTable.id == deployment_id)
        )
        return str(value) if value is not None else None

    def list(
        self,
        *,
        workspace_id: str,
        app_id: str | None = None,
        active: bool | None = None,
        include_deleted: bool = False,
    ) -> list[Deployment]:
        return self._list(
            workspace_id=workspace_id,
            app_id=app_id,
            active=active,
            include_deleted=include_deleted,
        )

    def list_across_workspaces(
        self,
        *,
        app_id: str | None = None,
        active: bool | None = None,
        include_deleted: bool = False,
    ) -> list[Deployment]:
        """Operator/system listing over every workspace's deployments."""
        return self._list(
            workspace_id=None,
            app_id=app_id,
            active=active,
            include_deleted=include_deleted,
        )

    def active_by_ids(self, deployment_ids: Sequence[str]) -> dict[str, bool]:
        wanted = tuple(dict.fromkeys(deployment_ids))
        active = {deployment_id: False for deployment_id in wanted}
        if not wanted:
            return active
        rows = self.session.scalars(
            select(DeploymentTable.id).where(
                DeploymentTable.id.in_(wanted),
                DeploymentTable.active.is_(True),
                DeploymentTable.deleted_at.is_(None),
            )
        )
        for deployment_id in rows:
            active[str(deployment_id)] = True
        return active

    def _list(
        self,
        *,
        workspace_id: str | None,
        app_id: str | None,
        active: bool | None,
        include_deleted: bool,
    ) -> list[Deployment]:
        statement = select(DeploymentTable)
        if workspace_id is not None:
            statement = statement.where(DeploymentTable.workspace_id == workspace_id)
        if app_id is not None:
            statement = statement.where(DeploymentTable.app_id == app_id)
        if active is not None:
            statement = statement.where(DeploymentTable.active.is_(active))
        if not include_deleted:
            statement = statement.where(DeploymentTable.deleted_at.is_(None))
        statement = statement.order_by(
            DeploymentTable.created_at.desc(),
            DeploymentTable.id.asc(),
        )
        return [deployment_from_table(row) for row in self.session.scalars(statement)]

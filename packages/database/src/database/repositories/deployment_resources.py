from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from database.mappers.apps import (
    app_record_from_table,
    deployment_from_table,
    stub_from_table,
)
from database.records.apps import (
    AppRecord,
    StubRecord,
)
from database.tables.apps import (
    AppTable,
    DeploymentTable,
    StubTable,
)
from foundation.ids import try_uuid
from pydantic import BaseModel
from shared.app_lifecycle import AppLifecycleState
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind
from sqlalchemy import (
    and_,
    case,
    func,
    or_,
    select,
    true,
    tuple_,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

type DeploymentCursor = tuple[str, str, int, datetime, str]


class DeploymentResourceRow(BaseModel):
    app: AppRecord
    deployment: Deployment
    stub: StubRecord


@dataclass(slots=True)
class DeploymentResourceRepository:
    session: Session

    def resolve(self, identifier: str, *, workspace_id: str | None) -> DeploymentResourceRow | None:
        scope = DeploymentTable.workspace_id == workspace_id if workspace_id is not None else true()
        deployment_id = try_uuid(identifier)
        if deployment_id is not None:
            row = self._resolve_host_row(
                and_(
                    DeploymentTable.id == deployment_id,
                    scope,
                )
            )
            if row is not None:
                return row
        return self._resolve_host_row(and_(DeploymentTable.name == identifier, scope))

    def invocation_state(self, stub: StubRecord) -> tuple[str, bool, str, int, bool] | None:
        row = self.session.execute(
            select(
                AppTable.name,
                AppTable.lifecycle_state == AppLifecycleState.Active.value,
                DeploymentTable.name,
                DeploymentTable.version,
                DeploymentTable.active,
            )
            .join(DeploymentTable, DeploymentTable.app_id == AppTable.id)
            .where(
                AppTable.workspace_id == stub.workspace_id,
                AppTable.deleted_at.is_(None),
                DeploymentTable.workspace_id == stub.workspace_id,
                DeploymentTable.id == stub.deployment_id,
                DeploymentTable.stub_id == stub.id,
                DeploymentTable.deleted_at.is_(None),
            )
        ).first()
        return row._tuple() if row is not None else None

    def summary_resources(
        self, *, workspace_id: str
    ) -> tuple[list[DeploymentResourceRow], dict[str, int]]:
        ranked = (
            select(
                DeploymentTable.id,
                func.row_number()
                .over(
                    partition_by=(
                        DeploymentTable.app_id,
                        DeploymentTable.kind,
                        DeploymentTable.name,
                    ),
                    order_by=(DeploymentTable.active.desc(), DeploymentTable.version.desc()),
                )
                .label("workload_rank"),
                func.row_number()
                .over(
                    partition_by=DeploymentTable.app_id,
                    order_by=(DeploymentTable.created_at.desc(), DeploymentTable.version.desc()),
                )
                .label("app_rank"),
                func.sum(case((DeploymentTable.active.is_(True), 1), else_=0))
                .over(
                    partition_by=DeploymentTable.app_id,
                )
                .label("active_versions"),
            )
            .where(
                DeploymentTable.workspace_id == workspace_id,
                DeploymentTable.deleted_at.is_(None),
                DeploymentTable.stub_id.is_not(None),
            )
            .subquery()
        )
        rows = self.session.execute(
            select(AppTable, DeploymentTable, StubTable, ranked.c.active_versions)
            .join(DeploymentTable, DeploymentTable.app_id == AppTable.id)
            .join(StubTable, StubTable.id == DeploymentTable.stub_id)
            .join(ranked, ranked.c.id == DeploymentTable.id)
            .where(
                AppTable.workspace_id == workspace_id,
                AppTable.deleted_at.is_(None),
                or_(ranked.c.workload_rank == 1, ranked.c.app_rank == 1),
            )
        )
        resources: list[DeploymentResourceRow] = []
        active_versions: dict[str, int] = {}
        for app, deployment, stub, active_count in rows:
            resources.append(
                DeploymentResourceRow(
                    app=app_record_from_table(app),
                    deployment=deployment_from_table(deployment),
                    stub=stub_from_table(stub),
                )
            )
            active_versions[str(app.id)] = active_count
        return resources, active_versions

    def list(
        self,
        *,
        workspace_id: str | None,
        app: str | None,
        app_id: str | None,
        deployment_id: str | None,
        name: str | None,
        kinds: frozenset[DeploymentKind] | set[DeploymentKind] | None,
        version: int | None,
        active: bool | None,
        latest_per_resource: bool = False,
        limit: int | None = None,
        after: DeploymentCursor | None = None,
    ) -> list[DeploymentResourceRow]:
        statement = (
            select(AppTable, DeploymentTable, StubTable)
            .join(DeploymentTable, DeploymentTable.app_id == AppTable.id)
            .join(StubTable, StubTable.id == DeploymentTable.stub_id)
            .where(AppTable.deleted_at.is_(None))
            .where(DeploymentTable.deleted_at.is_(None))
        )
        if workspace_id is not None:
            statement = statement.where(AppTable.workspace_id == workspace_id).where(
                DeploymentTable.workspace_id == workspace_id
            )
        if app is not None:
            statement = statement.where(AppTable.name == app)
        if app_id is not None:
            statement = statement.where(AppTable.id == app_id)
        if deployment_id is not None:
            statement = statement.where(DeploymentTable.id == deployment_id)
        if name is not None:
            statement = statement.where(DeploymentTable.name == name)
        if kinds is not None:
            statement = statement.where(
                DeploymentTable.kind.in_([deployment_kind.value for deployment_kind in kinds])
            )
        if version is not None:
            statement = statement.where(DeploymentTable.version == version)
        if active is not None:
            statement = statement.where(DeploymentTable.active.is_(active))
        if latest_per_resource:
            selected = (
                statement.with_only_columns(DeploymentTable.id)
                .distinct(DeploymentTable.app_id, DeploymentTable.kind, DeploymentTable.name)
                .order_by(
                    DeploymentTable.app_id,
                    DeploymentTable.kind,
                    DeploymentTable.name,
                    DeploymentTable.version.desc(),
                )
            )
            # Evaluate the version selection once before hydrating workload configs.
            latest = selected.cte("latest_deployments").prefix_with("MATERIALIZED")
            statement = statement.join(latest, latest.c.id == DeploymentTable.id)
        if after is not None:
            statement = statement.where(
                tuple_(
                    DeploymentTable.kind,
                    DeploymentTable.name,
                    -DeploymentTable.version,
                    DeploymentTable.created_at,
                    DeploymentTable.id,
                )
                > after
            )
        statement = statement.order_by(
            DeploymentTable.kind.asc(),
            DeploymentTable.name.asc(),
            DeploymentTable.version.desc(),
            DeploymentTable.created_at.asc(),
            DeploymentTable.id.asc(),
        )
        if latest_per_resource:
            statement = statement.order_by(None).order_by(
                DeploymentTable.name, DeploymentTable.version, DeploymentTable.id
            )
        if limit is not None:
            statement = statement.limit(limit)
        return [
            DeploymentResourceRow(
                app=app_record_from_table(app_row),
                deployment=deployment_from_table(deployment_row),
                stub=stub_from_table(stub_row),
            )
            for app_row, deployment_row, stub_row in self.session.execute(statement).tuples()
        ]

    def hostnames_claimed_under(self, *, workspace_id: str) -> list[str]:
        """Every hostname a live deployment in this workspace currently answers on.

        Read before retiring a registration, so discarding a certificate cannot take
        a serving deployment offline as a side effect.
        """
        rows = self.session.execute(
            select(DeploymentTable.custom_hostname)
            .where(DeploymentTable.workspace_id == workspace_id)
            .where(DeploymentTable.deleted_at.is_(None))
            .where(DeploymentTable.active.is_(True))
            .where(DeploymentTable.custom_hostname.is_not(None))
            .distinct()
        ).scalars()
        return [hostname for hostname in rows if hostname]

    def get_by_custom_hostname(self, hostname: str) -> DeploymentResourceRow | None:
        """Resolve the resource that claimed a registered hostname, at its latest version.

        Not workspace-scoped, for the same reason `get_by_subdomain` is not: the edge
        has only the hostname. Safe because a deployment may claim a hostname only
        under a domain its own workspace registered, and the registration is unique
        across workspaces.
        """
        return self._resolve_host_row(DeploymentTable.custom_hostname == hostname)

    def get_by_subdomain(
        self,
        subdomain: str,
        *,
        version: int | None = None,
    ) -> DeploymentResourceRow | None:
        """Resolve the resource a public hostname addresses.

        `version=None` answers with the latest, which is what a bare hostname means.

        Deliberately not workspace-scoped, unlike every other lookup here: a request
        arriving at the edge carries no token, so the subdomain is the only routing key
        available and the row it finds is what establishes which workspace answers.
        That is safe only because a subdomain belongs to exactly one resource, which
        `assert_subdomain_unclaimed` establishes when the subdomain is minted.
        """
        match = DeploymentTable.subdomain == subdomain
        if version is not None:
            match = and_(match, DeploymentTable.version == version)
        return self._resolve_host_row(match)

    def _resolve_host_row(self, match: ColumnElement[bool]) -> DeploymentResourceRow | None:
        row = (
            self.session.execute(
                select(AppTable, DeploymentTable, StubTable)
                .join(DeploymentTable, DeploymentTable.app_id == AppTable.id)
                .join(StubTable, StubTable.id == DeploymentTable.stub_id)
                .where(AppTable.deleted_at.is_(None))
                .where(DeploymentTable.deleted_at.is_(None))
                .where(match)
                .order_by(DeploymentTable.version.desc())
                .limit(1)
            )
            .tuples()
            .first()
        )
        if row is None:
            return None
        app_row, deployment_row, stub_row = row
        return DeploymentResourceRow(
            app=app_record_from_table(app_row),
            deployment=deployment_from_table(deployment_row),
            stub=stub_from_table(stub_row),
        )

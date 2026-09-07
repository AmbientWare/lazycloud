from __future__ import annotations

from dataclasses import dataclass

from database.records.apps import AppRecord, StubRecord
from database.repositories.apps import DeploymentResourceRepository, DeploymentResourceRow
from database.types import DatabaseSession
from shared.deployment_records import Deployment
from shared.deployments import DeploymentKind
from shared.errors import InvalidInputError, NotFoundError
from shared.http.client_manifests import ClientManifestResource, client_manifest_schemas
from shared.urls import StubUrlTarget, build_deployment_url, deployment_handler_path

from control.context import ControlContext


@dataclass(frozen=True, slots=True)
class DeploymentResource:
    app: AppRecord
    deployment: Deployment
    stub: StubRecord

    def invoke_url(self, external_url: str, *, pin_version: bool = False) -> str:
        target = StubUrlTarget(
            kind=self.stub.kind.value,
            stub_id=self.stub.id,
            deployment_name=self.deployment.name,
            deployment_version=self.deployment.version,
            subdomain=self.deployment.subdomain,
            public=self.stub.public or self.app.public,
        )
        try:
            return build_deployment_url(external_url, target, pin_version=pin_version)
        except ValueError as exc:
            raise InvalidInputError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class WorkloadSummary:
    resource: DeploymentResource
    version_count: int
    running_containers: int
    active_containers: int


def client_manifest_resource(
    resource: DeploymentResource,
    *,
    external_url: str,
) -> ClientManifestResource:
    """Invoke-facing view of one deployed resource: URL plus recorded schemas."""
    spec = resource.deployment.spec
    inputs, outputs = client_manifest_schemas(spec.metadata)
    return ClientManifestResource(
        app=resource.app.name,
        name=resource.deployment.name,
        kind=resource.deployment.kind,
        stub_id=resource.stub.id,
        deployment_id=resource.deployment.id,
        deployment_version=resource.deployment.version,
        invoke_url=resource.invoke_url(external_url),
        invoke_path=deployment_handler_path(
            resource.deployment.kind.value,
            resource.deployment.name,
        ),
        route=spec.route,
        methods=list(spec.methods),
        inputs=inputs,
        outputs=outputs,
        client_contract=spec.client_contract,
    )


@dataclass(slots=True)
class DeploymentResourceService:
    context: ControlContext

    def workloads(
        self,
        *,
        workspace: str,
        app_id: str,
        name: str | None = None,
        kind: DeploymentKind | None = None,
        after: str | None = None,
        limit: int = 50,
    ) -> list[WorkloadSummary]:
        with self.context.database.session() as session:
            workspace_id = self.context.workspace(session, workspace).id
            rows = DeploymentResourceRepository(session).workloads(
                workspace_id=workspace_id,
                app_id=app_id,
                name=name,
                kind=kind,
                after=after,
                limit=limit,
            )
            return [
                WorkloadSummary(
                    resource=_deployment_resource(row.resource),
                    version_count=row.version_count,
                    running_containers=row.running_containers,
                    active_containers=row.active_containers,
                )
                for row in rows
            ]

    def list(
        self,
        *,
        workspace: str | None = "default",
        app: str | None = None,
        app_id: str | None = None,
        deployment_id: str | None = None,
        name: str | None = None,
        kinds: frozenset[DeploymentKind] | set[DeploymentKind] | None = None,
        version: int | None = None,
        active: bool | None = True,
        latest_per_resource: bool = False,
    ) -> list[DeploymentResource]:
        with self.context.database.session() as session:
            return self.list_in_session(
                session,
                workspace=workspace,
                app=app,
                app_id=app_id,
                deployment_id=deployment_id,
                name=name,
                kinds=kinds,
                version=version,
                active=active,
                latest_per_resource=latest_per_resource,
            )

    def list_in_session(
        self,
        session: DatabaseSession,
        *,
        workspace: str | None = "default",
        app: str | None = None,
        app_id: str | None = None,
        deployment_id: str | None = None,
        name: str | None = None,
        kinds: frozenset[DeploymentKind] | set[DeploymentKind] | None = None,
        version: int | None = None,
        active: bool | None = True,
        latest_per_resource: bool = False,
    ) -> list[DeploymentResource]:
        workspace_id = (
            self.context.workspace(session, workspace).id if workspace is not None else None
        )
        resources = [
            _deployment_resource(row)
            for row in DeploymentResourceRepository(session).list(
                workspace_id=workspace_id,
                app=app,
                app_id=app_id,
                deployment_id=deployment_id,
                name=name,
                kinds=kinds,
                version=version,
                active=active,
            )
        ]
        if latest_per_resource:
            resources = _latest_per_resource(resources)
        resources.sort(
            key=lambda item: (
                item.deployment.kind.value,
                item.deployment.name,
                item.deployment.version,
            )
        )
        return resources

    def resolve_invoke_target(
        self,
        name: str,
        kind: DeploymentKind,
        *,
        workspace: str,
        version: int | None = None,
        app_id: str | None = None,
    ) -> DeploymentResource:
        """Resolve the deployment version an invoke URL targets.

        An unversioned invoke always targets the newest non-deleted version,
        even when that version has been stopped; a versioned invoke targets
        exactly that version. A stopped target is a client error, never a
        silent fallback to an older active version.
        """
        with self.context.database.session() as session:
            return self.resolve_invoke_target_in_session(
                session,
                name,
                kind,
                workspace=workspace,
                version=version,
                app_id=app_id,
            )

    def resolve_invoke_target_in_session(
        self,
        session: DatabaseSession,
        name: str,
        kind: DeploymentKind,
        *,
        workspace: str,
        version: int | None = None,
        app_id: str | None = None,
    ) -> DeploymentResource:
        candidates = self.list_in_session(
            session,
            workspace=workspace,
            app_id=app_id,
            name=name,
            kinds=frozenset({kind}),
            version=version,
            active=None,
        )
        if not candidates:
            raise NotFoundError(f"deployment not found: {name}")
        target = max(candidates, key=lambda item: item.deployment.version)
        if not target.deployment.active:
            msg = f"deployment is not active: {name} v{target.deployment.version}"
            raise InvalidInputError(msg)
        return target

    def get_by_deployment_id(
        self,
        deployment_id: str,
        *,
        workspace: str | None = "default",
    ) -> DeploymentResource | None:
        resources = self.list(
            workspace=workspace,
            deployment_id=deployment_id,
            active=None,
        )
        return resources[0] if resources else None

    def get_by_subdomain(
        self,
        subdomain: str,
        *,
        version: int | None = None,
    ) -> DeploymentResource | None:
        """Resolve the resource a public hostname addresses.

        The only lookup here that is not workspace-scoped, because a request at the
        public edge has no token to scope it by. The subdomain identifies exactly one
        resource, and the row it finds is what says which workspace owns the traffic.
        """

        with self.context.database.session() as session:
            return self.get_by_subdomain_in_session(session, subdomain, version=version)

    def get_by_subdomain_in_session(
        self,
        session: DatabaseSession,
        subdomain: str,
        *,
        version: int | None = None,
    ) -> DeploymentResource | None:
        row = DeploymentResourceRepository(session).get_by_subdomain(
            subdomain,
            version=version,
        )
        return _deployment_resource(row) if row is not None else None

    def get_by_custom_hostname(self, hostname: str) -> DeploymentResource | None:
        """Resolve the resource that claimed a registered hostname.

        Unscoped for the same reason as `get_by_subdomain`, and safe for the same
        reason: a hostname can only be claimed under a domain the claiming workspace
        registered, and a registration belongs to one workspace.
        """

        with self.context.database.session() as session:
            return self.get_by_custom_hostname_in_session(session, hostname)

    def get_by_custom_hostname_in_session(
        self,
        session: DatabaseSession,
        hostname: str,
    ) -> DeploymentResource | None:
        row = DeploymentResourceRepository(session).get_by_custom_hostname(hostname)
        return _deployment_resource(row) if row is not None else None


def _deployment_resource(row: DeploymentResourceRow) -> DeploymentResource:
    deployment = Deployment.model_validate(row.deployment_payload).model_copy(
        update={
            "app_id": row.deployment_app_id,
            "stub_id": row.deployment_stub_id,
        }
    )
    return DeploymentResource(
        app=row.app,
        deployment=deployment,
        stub=StubRecord.model_validate(row.stub_payload),
    )


def _latest_per_resource(resources: list[DeploymentResource]) -> list[DeploymentResource]:
    selected: dict[tuple[str, DeploymentKind], DeploymentResource] = {}
    for resource in resources:
        key = (resource.deployment.name, resource.deployment.kind)
        current = selected.get(key)
        if current is None or resource.deployment.version > current.deployment.version:
            selected[key] = resource
    return list(selected.values())

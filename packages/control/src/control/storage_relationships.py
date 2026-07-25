from __future__ import annotations

from dataclasses import dataclass

from shared.deployments import DeploymentKind
from shared.http.storage import ResourceWorkloadReference
from shared.mounts import MountAuthMode
from shared.workload_config import StubVolumeProviderConfig

from control.deployment_resources import DeploymentResource, DeploymentResourceService


@dataclass(frozen=True, slots=True)
class StorageResourceRelationships:
    secrets: dict[str, tuple[ResourceWorkloadReference, ...]]
    volumes: dict[str, tuple[ResourceWorkloadReference, ...]]


@dataclass(slots=True)
class StorageRelationshipService:
    deployments: DeploymentResourceService

    def for_workspace(self, workspace: str) -> StorageResourceRelationships:
        secret_uses: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]] = {}
        secret_active: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]] = {}
        volume_uses: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]] = {}
        volume_active: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]] = {}
        resources = self.deployments.list(workspace=workspace, active=None)
        for resource in resources:
            for secret_name in _secret_names(resource):
                _record_use(secret_uses, secret_active, secret_name, resource)
            for volume_name in {mount.name for mount in resource.deployment.spec.volumes}:
                _record_use(volume_uses, volume_active, volume_name, resource)
        return StorageResourceRelationships(
            secrets=_references(secret_uses, secret_active, resources),
            volumes=_references(volume_uses, volume_active, resources),
        )


def _secret_names(resource: DeploymentResource) -> set[str]:
    names = {name for name in resource.deployment.spec.secrets if name}
    for mount in resource.deployment.spec.volumes:
        if mount.config is None:
            continue
        config = StubVolumeProviderConfig.model_validate(mount.config)
        if config.auth_mode is not MountAuthMode.SecretReferences:
            continue
        names.update(name for name in (config.access_key, config.secret_key) if name)
    return names


def _record_use(
    uses: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]],
    active: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]],
    resource_name: str,
    resource: DeploymentResource,
) -> None:
    key = (
        resource.app.id,
        resource.deployment.name,
        resource.deployment.kind,
    )
    uses.setdefault(resource_name, {}).setdefault(key, set()).add(resource.deployment.version)
    if resource.deployment.active:
        active.setdefault(resource_name, {}).setdefault(key, set()).add(resource.deployment.version)


def _references(
    uses: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]],
    active: dict[str, dict[tuple[str, str, DeploymentKind], set[int]]],
    resources: list[DeploymentResource],
) -> dict[str, tuple[ResourceWorkloadReference, ...]]:
    app_names = {resource.app.id: resource.app.name for resource in resources}
    relationships: dict[str, tuple[ResourceWorkloadReference, ...]] = {}
    for resource_name, workloads in uses.items():
        references = [
            ResourceWorkloadReference(
                app_id=app_id,
                app_name=app_names[app_id],
                name=workload_name,
                kind=kind,
                versions=sorted(versions, reverse=True),
                active_versions=sorted(
                    active.get(resource_name, {}).get((app_id, workload_name, kind), set()),
                    reverse=True,
                ),
            )
            for (app_id, workload_name, kind), versions in workloads.items()
        ]
        references.sort(key=lambda item: (item.app_name, item.kind.value, item.name))
        relationships[resource_name] = tuple(references)
    return relationships


__all__ = ["StorageRelationshipService", "StorageResourceRelationships"]

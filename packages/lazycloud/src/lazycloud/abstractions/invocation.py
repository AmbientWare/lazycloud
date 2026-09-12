from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from shared.deployments import DeploymentKind

from lazycloud.abstractions.serve import PreviewSessionReader, read_serve_preview
from lazycloud.control import ControlClientConfig
from lazycloud.env import is_local
from lazycloud.session.deployment import DeploymentClient, DeploymentControlClient

INVOCATION_TARGET_AUTO = "auto"
INVOCATION_TARGET_SERVED = "served"
INVOCATION_TARGET_DEPLOYED = "deployed"
INVOCATION_TARGETS = {
    INVOCATION_TARGET_AUTO,
    INVOCATION_TARGET_SERVED,
    INVOCATION_TARGET_DEPLOYED,
}
InvocationTargetName: TypeAlias = Literal["auto", "served", "deployed"]


class InvocationTargetError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InvocationOptions:
    target: InvocationTargetName = "auto"
    deployment_name: str | None = None
    deployment_version: int | None = None


@dataclass(frozen=True, slots=True)
class InvocationTarget:
    kind: DeploymentKind
    source: str
    name: str
    workspace: str
    endpoint: str
    stub_id: str
    url: str
    deployment_id: str = ""
    deployment_version: int = 0


def resolve_invocation_target(
    *,
    kind: DeploymentKind,
    name: str,
    app: str,
    config: ControlClientConfig,
    deployment_client: DeploymentControlClient | None,
    preview_client: PreviewSessionReader,
    target: str = INVOCATION_TARGET_AUTO,
    deployment_name: str | None = None,
    deployment_version: int | None = None,
) -> InvocationTarget:
    if not app:
        msg = f"app is required to resolve {kind.value} target {name}"
        raise InvocationTargetError(msg)
    selected_target = _target(target)
    if deployment_version is not None and selected_target == INVOCATION_TARGET_SERVED:
        msg = "deployment_version can only be used with auto or deployed targets"
        raise InvocationTargetError(msg)

    if (
        deployment_version is None
        and selected_target in {INVOCATION_TARGET_AUTO, INVOCATION_TARGET_SERVED}
        and is_local()
    ):
        preview = read_serve_preview(
            kind=kind,
            name=name,
            app=app,
            workspace=config.workspace,
            endpoint=config.endpoint,
            client=preview_client,
        )
        if preview is not None:
            return InvocationTarget(
                kind=kind,
                source=INVOCATION_TARGET_SERVED,
                name=preview.name,
                workspace=preview.workspace,
                endpoint=preview.endpoint,
                stub_id=preview.stub_id,
                url=preview.url,
            )
        if selected_target == INVOCATION_TARGET_SERVED:
            msg = f"no active served {kind.value} target found for {name}"
            raise InvocationTargetError(msg)

    if selected_target in {INVOCATION_TARGET_AUTO, INVOCATION_TARGET_DEPLOYED}:
        selected_name = deployment_name or name
        try:
            resolved = DeploymentClient(
                client=deployment_client,
                workspace=config.workspace,
                endpoint=config.endpoint,
                token=config.token,
                timeout_seconds=config.timeout_seconds,
            ).resolve_target(
                kind=kind,
                name=selected_name,
                app=app,
                deployment_version=deployment_version,
                workspace=config.workspace,
            )
        except RuntimeError as exc:
            raise InvocationTargetError(str(exc)) from exc
        return InvocationTarget(
            kind=kind,
            source=INVOCATION_TARGET_DEPLOYED,
            name=resolved.deployment_name or selected_name,
            workspace=config.workspace,
            endpoint=config.endpoint,
            stub_id=resolved.stub_id,
            url=resolved.url,
            deployment_id=resolved.deployment_id,
            deployment_version=resolved.deployment_version,
        )

    msg = f"no invocation target found for {name}"
    raise InvocationTargetError(msg)


def _target(value: str) -> str:
    selected = value.strip().lower()
    if selected not in INVOCATION_TARGETS:
        msg = f"target must be one of: {', '.join(sorted(INVOCATION_TARGETS))}"
        raise InvocationTargetError(msg)
    return selected


__all__ = [
    "INVOCATION_TARGET_AUTO",
    "INVOCATION_TARGET_DEPLOYED",
    "INVOCATION_TARGET_SERVED",
    "InvocationOptions",
    "InvocationTarget",
    "InvocationTargetError",
    "InvocationTargetName",
    "resolve_invocation_target",
]

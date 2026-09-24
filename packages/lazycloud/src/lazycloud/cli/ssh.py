from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

import typer
from shared.deployments import DeploymentKind, DevboxPhase, PodRole
from shared.http.errors import HttpApiError

from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.output import emit
from lazycloud.cli.components.progress import ConnectingIndicator
from lazycloud.cli.control import control_config, resource_client, workspace_client
from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.clients.ssh.control import SshControlClient
from lazycloud.session.ssh import (
    SshAccess,
    SshPaths,
    SshPodHost,
    SshSetupError,
    bridge_stdio,
    current_cli_command,
    install_ssh_include,
    run_ssh,
)

_CONFLICT = 409

AppOption = Annotated[
    str | None,
    typer.Option("--app", help="App the pod belongs to; needed when several apps have the pod."),
]
WorkspaceOption = Annotated[str | None, typer.Option("--workspace")]


def ssh(
    ctx: typer.Context,
    pod: Annotated[str, typer.Argument(help="Devbox or pod to connect to.")],
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Open an SSH session to a devbox or pod. Arguments after `--` go to ssh."""
    with _setup_errors():
        access = _access(workspace)
        host = _pod_host(access, pod, app=app, workspace=workspace)
        access.write_hosts([host])
        access.refresh_certificate()
        status = run_ssh(access.paths.config, host.alias, list(ctx.args))
    raise typer.Exit(status)


def ssh_proxy(
    pod: Annotated[str, typer.Argument(help="Deployed pod to tunnel to.")],
    app: Annotated[str, typer.Option("--app", help="App the pod belongs to.")],
    workspace: WorkspaceOption = None,
) -> None:
    """Carry an SSH connection over stdin and stdout; used as ssh's ProxyCommand."""
    config = control_config(workspace=workspace)
    if not config.token:
        raise ClientError("not logged in; run `lazycloud login`", type="not_authenticated")
    client = SshControlClient.from_endpoint(config.endpoint, workspace=config.workspace)
    indicator = ConnectingIndicator(
        pod, describe=_DevboxPhase(resource_client(workspace=workspace), pod=pod, app=app)
    ).start()

    def report(message: str) -> None:
        if not indicator.failed(message):
            print(f"lazycloud: {message}", file=sys.stderr)

    try:
        status = bridge_stdio(
            client.tunnel_url(pod, app=app),
            token=config.token,
            on_first_byte=indicator.connected,
            on_failure=report,
        )
    finally:
        indicator.connected()
    raise typer.Exit(status)


_PHASE_LABELS: dict[DevboxPhase, str] = {
    DevboxPhase.Stopped: "waking up",
    DevboxPhase.Queued: "waiting for a machine",
    DevboxPhase.PullingImage: "pulling image",
    DevboxPhase.RestoringDisk: "restoring disk",
    DevboxPhase.Starting: "starting",
    DevboxPhase.Running: "connecting",
    DevboxPhase.Stopping: "saving disk",
}


@dataclass
class _DevboxPhase:
    """What a devbox reports doing, for the spinner; nothing for any other pod."""

    resources: ResourceControlClient
    pod: str
    app: str
    _deployment_id: str | None = None
    _resolved: bool = False

    def __call__(self) -> str | None:
        if not self._resolved:
            self._resolved = True
            self._deployment_id = self._find()
        if self._deployment_id is None:
            return None
        status = self.resources.devbox(self._deployment_id)
        if status.phase is DevboxPhase.Failed:
            return status.phase_reason
        return _PHASE_LABELS[status.phase]

    def _find(self) -> str | None:
        app_ids = {item.id for item in self.resources.list_apps().data if item.name == self.app}
        return next(
            (
                item.id
                for item in self.resources.list_deployments(
                    active=True, name=self.pod, latest=True
                ).data
                if item.kind is DeploymentKind.Pod
                and item.app_id in app_ids
                and item.role is PodRole.Devbox
            ),
            None,
        )


def ssh_cert(
    ctx: typer.Context,
    workspace: WorkspaceOption = None,
    quiet: Annotated[bool, typer.Option("--quiet", help="Print nothing on success.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Sign even if still fresh.")] = False,
) -> None:
    """Refresh the SSH certificate when it is missing or past half its lifetime."""
    with _setup_errors():
        access = _access(workspace)
        refreshed = access.refresh_certificate(force=force)
    if quiet:
        return
    certificate = access.paths.certificate(access.workspace)
    emit(
        ctx,
        payload={"certificate": str(certificate), "refreshed": refreshed},
        view=f"{'Signed' if refreshed else 'Still valid'}: {certificate}",
    )


def ssh_config(
    ctx: typer.Context,
    pods: Annotated[
        list[str] | None,
        typer.Argument(help="Devboxes and pods to configure; every one serving SSH when omitted."),
    ] = None,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Write ~/.lazycloud/ssh/config so ssh and editors reach devboxes and pods by name."""
    with _setup_errors():
        access = _access(workspace)
        access.ensure_key()
        if pods:
            hosts = [_pod_host(access, pod, app=app, workspace=workspace) for pod in pods]
        else:
            hosts = _ssh_enabled_pods(access, app=app, workspace=workspace)
        if not hosts:
            raise ClientError(
                "no deployed pod serves SSH",
                type="no_ssh_pods",
                hint="Deploy a devbox, or a pod with ssh=True, then run this again.",
            )
        access.write_hosts(hosts)
        access.refresh_certificate()
        included = install_ssh_include(access.paths)
    emit(
        ctx,
        payload={
            "config": str(access.paths.config),
            "hosts": [host.alias for host in hosts],
            "ssh_config_updated": included,
        },
        view="\n".join(
            [
                *(f"ssh {host.alias}" for host in hosts),
                f"Config: {access.paths.config}"
                + (" (included from ~/.ssh/config)" if included else ""),
            ]
        ),
    )


def _access(workspace: str | None) -> SshAccess:
    config = control_config(workspace=workspace)
    name = workspace or workspace_client().current().name
    return SshAccess(
        client=SshControlClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
            workspace=config.workspace,
        ),
        workspace=name,
        paths=SshPaths.current(),
        cli_command=current_cli_command(),
    )


def _pod_host(access: SshAccess, pod: str, *, app: str | None, workspace: str | None) -> SshPodHost:
    selected = app or _app_for_pod(resource_client(workspace=workspace), pod)
    return access.pod_host(pod, app=selected)


@contextmanager
def _setup_errors() -> Iterator[None]:
    try:
        yield
    except SshSetupError as exc:
        raise ClientError(str(exc), type="ssh_setup_failed") from exc


def _app_for_pod(resources: ResourceControlClient, pod: str) -> str:
    deployments = resources.list_deployments(active=True, name=pod, limit=100).data
    app_ids = {
        item.app_id for item in deployments if item.kind is DeploymentKind.Pod and item.app_id
    }
    if not app_ids:
        raise ClientError(f"no deployed pod named {pod!r}", type="pod_not_found")
    names = sorted(item.name for item in resources.list_apps().data if item.id in app_ids)
    if len(names) != 1:
        raise ClientError(
            f"pod {pod!r} is deployed in several apps: {', '.join(names)}",
            type="ambiguous_pod",
            hint="Name one with --app.",
        )
    return names[0]


def _ssh_enabled_pods(
    access: SshAccess,
    *,
    app: str | None,
    workspace: str | None,
) -> list[SshPodHost]:
    resources = resource_client(workspace=workspace)
    app_names = {item.id: item.name for item in resources.list_apps().data}
    pods: set[tuple[str, str]] = set()
    cursor: str | None = None
    while True:
        page = resources.list_deployments(active=True, limit=100, cursor=cursor)
        for item in page.data:
            name = app_names.get(item.app_id or "")
            if item.kind is DeploymentKind.Pod and name and (app is None or name == app):
                pods.add((name, item.name))
        if not page.next:
            break
        cursor = page.next
    hosts: list[SshPodHost] = []
    for app_name, pod in sorted(pods):
        try:
            hosts.append(access.pod_host(pod, app=app_name))
        except HttpApiError as exc:
            if exc.status_code != _CONFLICT:
                raise
    return hosts


__all__ = ["ssh", "ssh_cert", "ssh_config", "ssh_proxy"]

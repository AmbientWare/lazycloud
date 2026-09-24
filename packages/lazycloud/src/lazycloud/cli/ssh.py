from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

import typer
from shared.deployments import DeploymentKind, DevboxPhase, PodRole

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
    list_ssh_hosts,
    run_ssh,
)

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
        client = _client(workspace)
        listed = list_ssh_hosts(client, app=app, pod=pod)
        access = _access(client, listed.workspace)
        host = _one_host(listed.hosts, pod)
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
        access = _access(_client(workspace), workspace or workspace_client().current().name)
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
    prune: Annotated[
        bool,
        typer.Option(
            "--prune",
            "-p",
            help="Remove this workspace's hosts for pods that no longer serve SSH.",
        ),
    ] = False,
    app: AppOption = None,
    workspace: WorkspaceOption = None,
) -> None:
    """Write ~/.lazycloud/ssh/config so ssh and editors reach devboxes and pods by name."""
    if prune and pods:
        raise typer.BadParameter("--prune needs every host, so it takes no names")
    removed: list[str] = []
    with _setup_errors():
        client = _client(workspace)
        listed = list_ssh_hosts(client, app=app)
        access = _access(client, listed.workspace)
        hosts = [_one_host(listed.hosts, pod) for pod in pods] if pods else listed.hosts
        if not hosts and not prune:
            raise ClientError(
                "no devbox or pod serves SSH",
                type="no_ssh_pods",
                hint=(
                    "Deploy a devbox, or a pod with ssh=True. "
                    "`--prune` removes hosts left from earlier ones."
                ),
            )
        access.ensure_key()
        if prune:
            removed = access.sync_hosts(hosts, app=app)
        else:
            access.write_hosts(hosts)
        if hosts:
            access.refresh_certificate()
        included = install_ssh_include(access.paths) if hosts else False
    lines = [f"ssh {host.alias}" for host in hosts] or [
        f"No devbox or pod serves SSH in {access.workspace}."
    ]
    lines += [f"Removed {alias}" for alias in removed]
    lines.append(
        f"Config: {access.paths.config}" + (" (included from ~/.ssh/config)" if included else "")
    )
    emit(
        ctx,
        payload={
            "config": str(access.paths.config),
            "hosts": [host.alias for host in hosts],
            "removed": removed,
            "ssh_config_updated": included,
        },
        view="\n".join(lines),
    )


def _client(workspace: str | None) -> SshControlClient:
    config = control_config(workspace=workspace)
    return SshControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _access(client: SshControlClient, workspace_name: str) -> SshAccess:
    return SshAccess(
        client=client,
        workspace=workspace_name,
        paths=SshPaths.current(),
        cli_command=current_cli_command(),
    )


def _one_host(hosts: list[SshPodHost], pod: str) -> SshPodHost:
    matches = [host for host in hosts if host.pod == pod]
    if not matches:
        raise ClientError(f"no devbox or pod named {pod!r} serves SSH", type="pod_not_found")
    if len(matches) > 1:
        raise ClientError(
            f"{pod!r} is deployed in several apps: {', '.join(host.app for host in matches)}",
            type="ambiguous_pod",
            hint="Name one with --app.",
        )
    return matches[0]


@contextmanager
def _setup_errors() -> Iterator[None]:
    try:
        yield
    except SshSetupError as exc:
        raise ClientError(str(exc), type="ssh_setup_failed") from exc


__all__ = ["ssh", "ssh_cert", "ssh_config", "ssh_proxy"]

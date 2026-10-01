from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from typing import Annotated, Any
from uuid import UUID

import typer
from pydantic import TypeAdapter
from shared.api import (
    AwsConnection,
    AwsConnectionPhase,
    AwsConnectionRequest,
    AwsNetwork,
    AwsReconnectRequest,
    ComputeInstancePage,
    ComputeWorkloadPage,
    Container,
    ContainerState,
    MachineJoinRequest,
    MachineUpdate,
    StopReason,
)
from shared.http.gateway import CheckpointContainerRequest

from lazycloud._terminal.cards import notice_card, result_card
from lazycloud._terminal.formatting import duration
from lazycloud._terminal.streams import console
from lazycloud._terminal.theme import state_style, styled
from lazycloud.cli.components.output import (
    emit,
    json_default,
    json_output_enabled,
    print_payload,
    table,
    write_stream,
)
from lazycloud.cli.control import (
    api_session,
    gateway_client,
)
from lazycloud.cli.machine_join import agent_join_interrupted, build_machine_join_command
from lazycloud.clients.aws import create_connection_stack
from lazycloud.clients.compute import ComputeApi
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.session.task import follow_log_stream

container_app = typer.Typer(help="Inspect and manage containers.")
machine_app = typer.Typer(help="Manage self-hosted machines.")
cloud_app = typer.Typer(help="Connect and manage this account's cloud connection.")
cloud_connect_app = typer.Typer(help="Connect a cloud provider account.")
cloud_app.add_typer(cloud_connect_app, name="connect")
compute_app = typer.Typer(help="Inspect workspace compute capacity.")


def _compute(workspace: str | None = None) -> ComputeApi:
    return ComputeApi(api_client(resolve_control_client_config(workspace=workspace)))


def _workspace_compute(workspace: str | None) -> tuple[ComputeApi, str]:
    config = resolve_control_client_config(workspace=workspace)
    return ComputeApi(api_client(config)), require_workspace(config)


@compute_app.command("status", help="Show workspace compute capacity.")
def compute_status(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    compute, selected = _workspace_compute(workspace)
    response = compute.summary(selected)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    connection = response.connection
    instance_states = [f"{response.instances.ready} ready"]
    if response.instances.pending:
        instance_states.append(f"{response.instances.pending} pending")
    if response.instances.degraded:
        instance_states.append(f"{response.instances.degraded} degraded")
    console.print(
        result_card(
            {
                "cloud": connection.account_id if connection is not None else "not connected",
                "instances": ", ".join(instance_states),
                "workloads": response.workload_count,
            },
        )
    )


@compute_app.command("instances", help="List provisioned compute instances.")
def compute_instances(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    instances = _compute(workspace).instances()
    if json_output_enabled(ctx):
        print_payload(ctx, ComputeInstancePage(instances=instances).model_dump(mode="json"))
        return
    rows = [
        [
            item.provider,
            item.region,
            item.instance_type,
            item.lifecycle.value,
            item.lifecycle_failure.value if item.lifecycle_failure is not None else "",
            item.lifecycle_message,
        ]
        for item in instances
    ]
    console.print(
        table(
            "Compute instances",
            ["provider", "region", "type", "lifecycle", "failure", "detail"],
            rows,
        )
    )


@compute_app.command("workloads", help="List workloads and the machine each is pinned to.")
def compute_workloads(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    compute, selected = _workspace_compute(workspace)
    workloads = compute.workloads(selected)
    if json_output_enabled(ctx):
        print_payload(ctx, ComputeWorkloadPage(workloads=workloads).model_dump(mode="json"))
        return
    rows = [[item.name, item.kind, item.machine] for item in workloads]
    console.print(table("Compute workloads", ["name", "kind", "machine"], rows))


@cloud_connect_app.command("aws")
def cloud_connect_aws(
    ctx: typer.Context,
    account_id: Annotated[str, typer.Option("--account-id", help="AWS account ID.")],
    role_arn: Annotated[
        str | None,
        typer.Option("--role-arn", help="Existing cross-account management role."),
    ] = None,
    networks_json: Annotated[
        str,
        typer.Option(
            "--networks-json",
            help="Regional VPC, subnet and security group IDs as JSON; requires an existing role.",
        ),
    ] = "{}",
) -> None:
    """Connect an AWS account, which backs every workspace you own."""
    networks = TypeAdapter(dict[str, AwsNetwork]).validate_json(networks_json)
    fields: dict[str, object] = {"account_id": account_id}
    if role_arn is not None:
        fields["role_arn"] = role_arn
    if networks:
        fields["networks"] = networks
    response = _compute().connect_aws(AwsConnectionRequest.model_validate(fields))
    if response.authorization.stack is None and response.authorization.external_id is None:
        raise RuntimeError("existing-role authorization did not return its external ID")
    authorization: dict[str, object] = {
        "account_id": account_id,
        "phase": response.connection.phase.value,
    }
    if response.authorization.stack:
        authorization["authorize"] = "Run `lazycloud cloud authorize --profile YOUR_AWS_PROFILE`."
    if response.authorization.external_id:
        authorization["external_id"] = response.authorization.external_id
    authorization["next_step"] = "Run `lazycloud cloud validate` after the AWS stack finishes."
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(authorization),
            title="AWS authorization required",
            tone="info",
        ),
    )


@cloud_app.command("reconnect")
def cloud_reconnect(
    ctx: typer.Context,
    role_arn: Annotated[
        str | None,
        typer.Option("--role-arn", help="Connected enterprise role to revalidate."),
    ] = None,
) -> None:
    """Start replacement authorization for the connected account."""
    response = _compute().reconnect_aws(
        AwsReconnectRequest(role_arn=role_arn) if role_arn is not None else AwsReconnectRequest()
    )
    account_id = response.connection.account_id
    authorization: dict[str, object] = {
        "account_id": account_id,
        "phase": response.connection.phase.value,
    }
    if response.authorization.stack:
        authorization["authorize"] = "Run `lazycloud cloud authorize --profile YOUR_AWS_PROFILE`."
    if response.authorization.external_id:
        authorization["external_id"] = response.authorization.external_id
    authorization["next_step"] = "Run `lazycloud cloud validate` after the AWS stack finishes."
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(authorization),
            title="Replacement authorization required",
            tone="info",
        ),
    )


@cloud_app.command("authorize")
def cloud_authorize(
    ctx: typer.Context,
    profile: Annotated[
        str | None, typer.Option("--profile", help="Customer AWS CLI profile.")
    ] = None,
) -> None:
    """Create the pending IAM connection stack in your AWS account."""
    connection = _compute().aws_connection()
    action = connection.customer_action if connection is not None else None
    if action is None or action.stack is None:
        raise RuntimeError("there is no pending AWS connection stack to authorize")
    stack_id = create_connection_stack(action.stack, profile=profile)
    emit(
        ctx,
        payload={"stack_id": stack_id, "status": "CREATE_IN_PROGRESS"},
        view=result_card(
            "Run `lazycloud cloud validate` after the stack finishes in CloudFormation.",
            title="AWS connection stack submitted",
            tone="info",
        ),
    )


@cloud_app.command("validate")
def cloud_validate(
    ctx: typer.Context,
) -> None:
    """Validate the pending or active cloud authorization."""
    response = _compute().validate_aws()
    account_id = response.account_id
    failure = _aws_validation_failure(response)
    summary: dict[str, object] = {
        "account_id": account_id,
        "phase": response.phase.value,
    }
    if failure is not None:
        summary["issue"] = f"{failure[0]}: {failure[1]}"
    elif response.detail:
        summary["detail"] = response.detail
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(summary),
            title="AWS authorization validated" if failure is None else "AWS validation failed",
            tone="success" if failure is None else "warning",
        ),
    )
    if failure is not None:
        raise typer.Exit(code=1)


@cloud_app.command("status")
def cloud_status(
    ctx: typer.Context,
    watch: Annotated[bool, typer.Option("--watch", help="Wait for a connection phase.")] = False,
    until: Annotated[
        AwsConnectionPhase | None,
        typer.Option("--until", help="Connection phase to wait for."),
    ] = None,
    interval_seconds: Annotated[float, typer.Option("--interval", min=0.2)] = 2.0,
    timeout_seconds: Annotated[float, typer.Option("--timeout", min=1.0)] = 600.0,
) -> None:
    """Show the workspace's cloud connection status."""
    if until is not None and not watch:
        raise typer.BadParameter("--until requires --watch")
    if watch and until is None:
        raise typer.BadParameter("--watch requires --until")
    compute = _compute()
    response = compute.aws_connection()
    if response is None:
        emit(
            ctx,
            payload={"connection": None},
            view=notice_card(
                "No cloud account is connected.",
                tone="neutral",
            ),
        )
        return
    account_id = response.account_id
    if watch:
        target_phase = until
        if target_phase is None:
            raise typer.BadParameter("--watch requires --until")
        deadline = time.monotonic() + timeout_seconds
        while response.phase is not target_phase:
            _print_cloud_poll(account_id, response, started_at=deadline - timeout_seconds)
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"timed out waiting for AWS account {account_id} to reach {target_phase.value}"
                )
            time.sleep(interval_seconds)
            current = compute.aws_connection()
            if current is None:
                raise RuntimeError("the AWS account connection was removed while waiting")
            response = current
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(json_default(_connection_summary(response))),
    )


@cloud_app.command("disconnect")
def cloud_disconnect(
    ctx: typer.Context,
    open_console: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="Open AWS only when automatic removal requires recovery.",
        ),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", help="Wait for the connection to be fully removed."),
    ] = False,
    interval_seconds: Annotated[float, typer.Option("--interval", min=0.2)] = 2.0,
    timeout_seconds: Annotated[float, typer.Option("--timeout", min=1.0)] = 600.0,
) -> None:
    """Disconnect the cloud account and remove its managed compute."""
    compute = _compute()
    current = compute.aws_connection()
    if current is None:
        raise RuntimeError("this workspace does not have an AWS account connection")
    account_id = current.account_id
    connection = compute.disconnect_aws()
    if wait and connection is not None:
        deadline = time.monotonic() + timeout_seconds
        while connection is not None and connection.phase is not AwsConnectionPhase.action_required:
            _print_cloud_poll(account_id, connection, started_at=deadline - timeout_seconds)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out waiting for AWS account {account_id} removal")
            time.sleep(interval_seconds)
            connection = compute.aws_connection()
    payload: dict[str, object] = {
        "connection": connection.model_dump(mode="json") if connection is not None else None
    }
    summary = (
        {"account_id": account_id, "status": "removed"}
        if connection is None
        else _connection_summary(connection)
    )
    emit(
        ctx,
        payload=payload,
        view=result_card(
            json_default(summary),
            title="AWS account removed" if connection is None else "AWS disconnect status",
            tone="success" if connection is None else "info",
        ),
    )
    if connection is None:
        return
    if connection.phase is AwsConnectionPhase.action_required:
        if (
            open_console
            and connection.customer_action is not None
            and connection.customer_action.url is not None
        ):
            webbrowser.open(connection.customer_action.url)
        return


@cloud_app.command("cancel-reconnect")
def cloud_cancel_reconnect(
    ctx: typer.Context,
) -> None:
    """Cancel a pending replacement authorization."""
    response = _compute().cancel_aws_reconnect()
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(_connection_summary(response)),
            title="Reconnect cancelled",
            tone="success",
        ),
    )


@cloud_app.command("retry")
def cloud_retry(
    ctx: typer.Context,
) -> None:
    """Retry the connection's current pending action."""
    response = _compute().retry_aws()
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(_connection_summary(response)),
            title="Cloud action retried",
            tone="info",
        ),
    )


def _aws_validation_failure(response: AwsConnection) -> tuple[str, str] | None:
    for authorization in (response.pending_authorization, response.active_authorization):
        if authorization is not None and authorization.error_code is not None:
            return authorization.error_code.value, authorization.error_message or response.detail
    return None


def _connection_summary(response: AwsConnection) -> dict[str, object]:
    summary: dict[str, object] = {
        "account_id": response.account_id,
        "phase": response.phase.value,
    }
    if response.detail:
        summary["detail"] = response.detail
    if response.customer_action is not None:
        summary["action"] = response.customer_action.label
        summary["action_url"] = response.customer_action.url
    return summary


def _print_cloud_poll(
    account_id: str,
    response: AwsConnection,
    *,
    started_at: float,
) -> None:
    elapsed = duration(time.monotonic() - started_at)
    status = styled(response.phase.value.replace("_", " "), state_style(response.phase))
    console.print(f"[{elapsed}] {account_id}", status, response.detail, soft_wrap=True)


@container_app.command("list", help="List recent containers.")
def container_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    containers: list[Container] = []
    cursor: str | None = None
    while len(containers) < limit:
        page = client.list_containers(
            selected_workspace, limit=limit - len(containers), cursor=cursor
        )
        containers.extend(page.containers)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in containers])
        return
    rows: list[list[Any]] = [
        [
            item.function,
            item.state.value,
            item.stop_reason.value if item.stop_reason is not None else None,
            str(item.id),
        ]
        for item in containers
    ]
    console.print(table("Containers", ["name", "status", "exit", "id"], rows))


@container_app.command("attach", help="Attach to a container's output until it exits.")
def container_attach(
    ctx: typer.Context,
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    container = _container_uuid(container_id)
    chunks: list[str] = []
    json_output = json_output_enabled(ctx)
    for entry in follow_log_stream(
        lambda after: client.stream_container_logs(
            selected_workspace, container, after=after, follow=True
        )
    ):
        line = entry.data if entry.data.endswith("\n") else f"{entry.data}\n"
        chunks.append(line)
        if not json_output:
            write_stream(line)
    finished = client.get_container(selected_workspace, container)
    if finished.state is not ContainerState.stopped or finished.stop_reason is None:
        raise typer.BadParameter("container attach stream ended before the container completed")
    reason = finished.stop_reason
    emit(
        ctx,
        payload={**finished.model_dump(mode="json"), "output": "".join(chunks)},
        view=result_card(
            {"exit": reason.value},
            title="Container finished",
            tone="success" if reason is StopReason.stopped else "warning",
        ),
    )
    if reason is not StopReason.stopped:
        raise typer.Exit(1)


@container_app.command("checkpoint", help="Create a container checkpoint.")
def container_checkpoint(
    ctx: typer.Context,
    container_id: str,
    checkpoint_id: Annotated[str | None, typer.Option("--checkpoint-id")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = gateway_client(workspace=workspace).checkpoint_container(
        CheckpointContainerRequest(container_id=container_id, checkpoint_id=checkpoint_id)
    )
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            f"Created checkpoint {response.checkpoint_id}.",
            tone="success",
        ),
    )


@container_app.command("stop", help="Stop one or more containers.")
def container_stop(
    ctx: typer.Context,
    container_ids: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client, selected_workspace = api_session(workspace=workspace)
    results: list[dict[str, object]] = []
    for container_id in container_ids:
        client.stop_container(selected_workspace, _container_uuid(container_id))
        results.append({"container_id": container_id})
    emit(
        ctx,
        payload=results,
        view=notice_card(
            f"Stopped {len(results)} container{'s' if len(results) != 1 else ''}.",
            tone="success",
        ),
    )


def _container_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise typer.BadParameter(f"not a container id: {value}") from None


@machine_app.command("list", help="List joined machines.")
def machine_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """List the machines this account has joined."""
    compute, selected = _workspace_compute(workspace)
    machines = compute.machines(selected)
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in machines])
        return
    rows: list[list[str]] = [
        [item.name, ", ".join(item.workspaces), item.lifecycle.value, item.gpu, str(item.id)]
        for item in machines
    ]
    console.print(table("Machines", ["name", "workspaces", "lifecycle", "gpu", "id"], rows))


@machine_app.command("update", help="Change the workspaces a joined machine serves.")
def machine_update(
    ctx: typer.Context,
    machine: Annotated[str, typer.Argument(help="Machine name or ID.")],
    workspaces: Annotated[
        list[str],
        typer.Option(
            "--workspaces",
            help="Workspace names whose workloads may run on this machine, comma-separated.",
        ),
    ],
) -> None:
    names = _workspace_names(workspaces)
    if not names:
        raise typer.BadParameter("--workspaces needs at least one workspace name")
    response = _compute().update_machine(
        machine, MachineUpdate.model_validate({"workspaces": names})
    )
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card({"name": response.name, "workspaces": ", ".join(response.workspaces)}),
    )


@machine_app.command("join", help="Join this machine to your account.")
def machine_join(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Option("--name", help="Name workloads pin to; unique across the account."),
    ],
    workspaces: Annotated[
        list[str],
        typer.Option(
            "--workspaces",
            help="Workspace names whose workloads may run on this machine, comma-separated.",
        ),
    ],
    gpu: Annotated[
        list[str] | None,
        typer.Option("--gpu", help="GPU type this machine contributes."),
    ] = None,
    max_cpu: Annotated[
        str,
        typer.Option("--max-cpu", help="Maximum CPU cores to advertise."),
    ] = "",
    max_memory: Annotated[
        str,
        typer.Option("--max-memory", help="Maximum memory to advertise, for example 32Gi."),
    ] = "",
    max_gpus: Annotated[
        int,
        typer.Option("--max-gpus", min=0, help="Maximum GPUs to advertise."),
    ] = 0,
    gpu_ids: Annotated[
        str,
        typer.Option("--gpu-ids", help="Comma-separated GPU device IDs to expose."),
    ] = "",
    background: Annotated[
        bool,
        typer.Option(
            "--background/--foreground",
            help="Install a background service. Runs in the foreground by default.",
        ),
    ] = False,
    service_manager: Annotated[
        str,
        typer.Option("--service-manager", help="Service manager for background installs."),
    ] = "",
    service_name: Annotated[
        str,
        typer.Option("--service-name", help="Background service name."),
    ] = "",
    state_dir: Annotated[
        str,
        typer.Option("--state-dir", help="Agent state directory."),
    ] = "",
) -> None:
    """Join this machine to your account under a name workloads can pin to.

    The host belongs to the account and runs workloads only for the workspaces
    named here.
    """
    if gpu_ids and max_gpus:
        raise typer.BadParameter("--gpu-ids and --max-gpus cannot both be set")
    workspace_names = _workspace_names(workspaces)
    if not workspace_names:
        raise typer.BadParameter("--workspaces needs at least one workspace name")

    response = _compute().join_command(
        MachineJoinRequest.model_validate(
            {"name": name, "workspaces": workspace_names, "gpu": list(gpu or [])}
        )
    )
    command = build_machine_join_command(
        response.command,
        max_cpu=max_cpu,
        max_memory=max_memory,
        max_gpus=max_gpus,
        gpu_ids=gpu_ids,
        background=background,
        service_manager=service_manager,
        service_name=service_name,
        state_dir=state_dir,
    )
    try:
        exit_code = subprocess.call(
            command, shell=True, stdout=sys.stderr if json_output_enabled(ctx) else None
        )
    except KeyboardInterrupt:
        return
    if agent_join_interrupted(exit_code):
        return
    if exit_code:
        raise typer.Exit(exit_code)
    emit(
        ctx,
        payload={"status": "running"},
        view=notice_card("The agent is running.", title="Machine joined", tone="success"),
    )


@machine_app.command("remove", help="Remove a joined machine.")
def machine_remove(
    ctx: typer.Context,
    machine_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Remove a machine this account joined."""
    _compute(workspace).remove_machine(machine_id)
    emit(
        ctx,
        payload={"machine_id": machine_id, "removed": True},
        view=notice_card(f"Removed {machine_id}.", tone="success"),
    )


def _workspace_names(values: list[str]) -> list[str]:
    names: list[str] = []
    for value in values:
        for name in value.split(","):
            name = name.strip()
            if name and name not in names:
                names.append(name)
    return names

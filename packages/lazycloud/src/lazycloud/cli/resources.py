from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import TypeAdapter
from shared.aws_connections import (
    AwsAccountConnectionPhase,
    AwsAccountNetwork,
    AwsRegion,
)
from shared.http.aws_connections import (
    AwsConnectionResponse,
)
from shared.http.compute import (
    ContainerResponse,
    MachineJoinCommandRequest,
)
from shared.http.gateway import (
    AttachToContainerResponse,
    CheckpointContainerRequest,
)
from shared.tasks import is_terminal_task_status

from lazycloud.cli.apps import resolve_app_id
from lazycloud.cli.components.cards import empty_state, notice_card, result_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.formatting import duration, timestamp
from lazycloud.cli.components.output import (
    console,
    emit,
    error_console,
    json_default,
    json_output_enabled,
    print_payload,
    table,
    write_stream,
)
from lazycloud.cli.components.theme import MUTED, state_style, styled
from lazycloud.cli.control import (
    compute_client,
    gateway_client,
    resource_client,
    task_client,
)
from lazycloud.cli.machine_join import agent_join_interrupted, build_machine_join_command
from lazycloud.cli.result_output import task_result_export, task_result_view
from lazycloud.clients.aws import create_connection_stack

task_app = typer.Typer(help="Inspect and manage tasks.")
container_app = typer.Typer(help="Inspect and manage containers.")
machine_app = typer.Typer(help="Manage self-hosted machines.")
cloud_app = typer.Typer(help="Connect and manage this account's cloud connection.")
cloud_connect_app = typer.Typer(help="Connect a cloud provider account.")
cloud_app.add_typer(cloud_connect_app, name="connect")
compute_app = typer.Typer(help="Inspect workspace compute capacity.")


@compute_app.command("status", help="Show workspace compute capacity.")
def compute_status(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).summary()
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
    response = compute_client(workspace=workspace).instances()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [
        [
            item.provider,
            item.region,
            item.instance_type or "",
            item.service_state.value,
            item.bootstrap_failure_detail
            or (
                item.bootstrap_failure_reason.value
                if item.bootstrap_failure_reason is not None
                else ""
            ),
        ]
        for item in response.data
    ]
    console.print(
        table(
            "Compute instances",
            ["provider", "region", "type", "state", "issue"],
            rows,
        )
    )


@compute_app.command("workloads", help="List workloads and the machine each is pinned to.")
def compute_workloads(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).workloads()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [[item.name, item.kind.value, item.machine] for item in response.data]
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
    networks = TypeAdapter(dict[AwsRegion, AwsAccountNetwork]).validate_json(networks_json)
    response = compute_client().connect_account(
        account_id=account_id,
        role_arn=role_arn,
        networks=networks,
    )
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
    response = compute_client().reconnect_account(role_arn=role_arn)
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
    connection = compute_client().current_connection()
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
    response = compute_client().validate_connection()
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
        AwsAccountConnectionPhase | None,
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
    client = compute_client()
    response = client.current_connection()
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
            current = client.current_connection()
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
    client = compute_client()
    current = client.current_connection()
    if current is None:
        raise RuntimeError("this workspace does not have an AWS account connection")
    account_id = current.account_id
    connection = client.remove_account()
    if wait and connection is not None:
        deadline = time.monotonic() + timeout_seconds
        while (
            connection is not None
            and connection.phase is not AwsAccountConnectionPhase.ActionRequired
        ):
            _print_cloud_poll(account_id, connection, started_at=deadline - timeout_seconds)
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out waiting for AWS account {account_id} removal")
            time.sleep(interval_seconds)
            connection = client.current_connection()
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
    if connection.phase is AwsAccountConnectionPhase.ActionRequired:
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
    response = compute_client().cancel_reconnect()
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
    response = compute_client().retry_connection()
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(_connection_summary(response)),
            title="Cloud action retried",
            tone="info",
        ),
    )


def _aws_validation_failure(response: AwsConnectionResponse) -> tuple[str, str] | None:
    for authorization in (response.pending_authorization, response.active_authorization):
        if authorization is not None and authorization.error_code is not None:
            return authorization.error_code.value, authorization.error_message or response.detail
    return None


def _connection_summary(response: AwsConnectionResponse) -> dict[str, object]:
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
    response: AwsConnectionResponse,
    *,
    started_at: float,
) -> None:
    elapsed = duration(time.monotonic() - started_at)
    status = styled(response.phase.value.replace("_", " "), state_style(response.phase))
    console.print(f"[{elapsed}] {account_id}", status, response.detail, soft_wrap=True)


@task_app.command("list", help="List recent tasks.")
def task_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 100,
    app: Annotated[
        str | None,
        typer.Option("--app", help="App name or ID."),
    ] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = resource_client(workspace=workspace)
    app_id = resolve_app_id(app, client=client) if app else None
    response = client.list_tasks(limit=limit, app_id=app_id)
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in response.data])
        return
    rows: list[list[str]] = [
        [
            item.workload.name if item.workload is not None else item.name,
            item.status.value,
            timestamp(item.created_at),
            item.id,
        ]
        for item in response.data
    ]
    console.print(table("Tasks", ["workload", "status", "requested", "id"], rows))


@task_app.command("stop", help="Stop one or more tasks.")
def task_stop(
    ctx: typer.Context,
    task_ids: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).stop_tasks(task_ids)
    summary: dict[str, object] = {"stopped": len(response.stopped)}
    if response.skipped:
        summary["skipped"] = list(response.skipped)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            json_default(summary),
            title="Tasks stopped",
            tone="warning" if response.skipped else "success",
        ),
    )


@task_app.command("show", help="Show one task and its current state.")
def task_show(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    task = task_client(workspace=workspace).detail(task_id)
    task_name = task.workload.name if task.workload is not None else task.name
    summary: dict[str, object] = {
        "workload": task_name,
        "status": task.status.value,
        "requested": timestamp(task.created_at),
    }
    if task.max_attempts > 1:
        summary["attempt"] = f"{max(task.attempt_number, 1)} of {task.max_attempts}"
    if task.started_at is not None:
        summary["started"] = timestamp(task.started_at)
    if task.finished_at is not None:
        summary["finished"] = timestamp(task.finished_at)
    if task.container_id:
        summary["container"] = task.container_id
    if task.error:
        summary["error"] = task.error
    emit(
        ctx,
        payload=task.model_dump(mode="json"),
        view=result_card(json_default(summary)),
    )


@task_app.command("result", help="Wait for and display a task result.")
def task_result(
    ctx: typer.Context,
    task_id: str,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    timeout_seconds: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Save the result to a .png, .html, .txt, .json, or .pkl file.",
            dir_okay=False,
            writable=True,
        ),
    ] = None,
) -> None:
    client = task_client(workspace=workspace)
    if wait and not json_output_enabled(ctx):
        with console.status(f"Waiting for task {task_id}…"):
            result = client.handle(task_id).result(
                wait=True,
                timeout_seconds=timeout_seconds,
            )
    else:
        result = client.handle(task_id).result(
            wait=wait,
            timeout_seconds=timeout_seconds,
        )
    task = client.detail(task_id)
    if result.ok:
        if output is not None:
            task_result_export(task).write(output)
        emit(ctx, payload=task.model_dump(mode="json"), view=task_result_view(task))
        if output is not None and not json_output_enabled(ctx):
            error_console.print(styled(f"Saved {output}", MUTED))
        return
    if is_terminal_task_status(result.status):
        raise ClientError(
            result.error or f"task {task_id} finished with status {result.status.value}",
            type="task_failed",
            title="Task failed",
            exit_code=result.exit_code or 1,
        )
    emit(
        ctx,
        payload=task.model_dump(mode="json"),
        view=notice_card(
            f"Task {task_id} is {task.status.value.replace('_', ' ')}.",
            hint=f"Run `lazycloud task result {task_id}` to wait for it.",
        ),
    )


@task_app.command("logs", help="Print logs for one task.")
def task_logs(
    ctx: typer.Context,
    task_id: str,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 250,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    logs = task_client(workspace=workspace).logs(task_id, limit=limit)
    if json_output_enabled(ctx):
        print_payload(ctx, [entry.model_dump(mode="json") for entry in logs])
        return
    if not logs:
        console.print(empty_state("No log entries found."))
        return
    for entry in logs:
        write_stream(entry.message if entry.message.endswith("\n") else f"{entry.message}\n")


@task_app.command("cancel", help="Cancel a task.")
def task_cancel(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = task_client(workspace=workspace).cancel(task_id)
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            f"Cancelled task {task_id}.",
            tone="success",
        ),
    )


@container_app.command("list", help="List recent containers.")
def container_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = resource_client(workspace=workspace)
    containers: list[ContainerResponse] = []
    cursor = ""
    seen_cursors: set[str] = set()
    while len(containers) < limit:
        response = client.list_containers(
            limit=min(100, limit - len(containers)),
            cursor=cursor or None,
        )
        containers.extend(item.container for item in response.data)
        if not response.next or response.next in seen_cursors:
            break
        seen_cursors.add(response.next)
        cursor = response.next
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in containers])
        return
    rows: list[list[Any]] = [
        [item.name, item.status.value, item.exit_code, item.id] for item in containers
    ]
    console.print(table("Containers", ["name", "status", "exit", "id"], rows))


@container_app.command("attach", help="Attach to a container's output until it exits.")
def container_attach(
    ctx: typer.Context,
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    chunks: list[str] = []
    terminal: AttachToContainerResponse | None = None
    json_output = json_output_enabled(ctx)
    for response in gateway_client(workspace=workspace).attach_to_container_events(container_id):
        if response.error_msg:
            raise typer.BadParameter(response.error_msg)
        if response.output:
            chunks.append(response.output)
            if not json_output:
                write_stream(response.output)
        if response.done:
            terminal = response
            break
    if terminal is None:
        raise typer.BadParameter("container attach stream ended before the container completed")
    terminal = terminal.model_copy(update={"output": "".join(chunks)})
    emit(
        ctx,
        payload=terminal.model_dump(mode="json"),
        view=result_card(
            {"exit_code": terminal.exit_code if terminal.exit_code is not None else "Not reported"},
            title="Container finished",
            tone="success" if terminal.exit_code == 0 else "warning",
        ),
    )
    if terminal.exit_code != 0:
        raise typer.Exit(terminal.exit_code if terminal.exit_code is not None else 1)


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
    client = resource_client(workspace=workspace)
    results: list[dict[str, object]] = []
    for container_id in container_ids:
        client.stop_container(container_id)
        results.append({"container_id": container_id})
    emit(
        ctx,
        payload=results,
        view=notice_card(
            f"Stopped {len(results)} container{'s' if len(results) != 1 else ''}.",
            tone="success",
        ),
    )


@machine_app.command("list", help="List joined machines.")
def machine_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """List the machines this account has joined."""
    machines = resource_client(workspace=workspace).list_machines().machines
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in machines])
        return
    rows: list[list[str]] = [
        [item.name, ", ".join(item.workspaces), item.status.value, item.gpu or "", item.id]
        for item in machines
    ]
    console.print(table("Machines", ["name", "workspaces", "status", "gpu", "id"], rows))


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
    machines = resource_client().list_machines().machines
    matches = [item for item in machines if machine in (item.name, item.id)]
    if not matches:
        raise ClientError(f"No joined machine is named {machine}.")
    response = compute_client().update_machine(matches[0].id, workspaces=names)
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
    ttl: Annotated[str, typer.Option("--ttl", help="Join token lifetime.")] = "",
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

    response = compute_client().machine_join_command(
        MachineJoinCommandRequest(
            ttl=ttl,
            name=name,
            workspaces=workspace_names,
            gpu=list(gpu or []),
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
    compute_client(workspace=workspace).remove_machine(machine_id)
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

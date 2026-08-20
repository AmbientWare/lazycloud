from __future__ import annotations

import subprocess
import time
import webbrowser
from typing import Annotated, Any

import typer
from shared.aws_connections import AwsAccountConnectionPhase, AwsAccountNetwork
from shared.compute_policy import MachinePool
from shared.http.aws_connections import (
    AwsComputeConfigurationUpdateRequest,
    AwsConnectionResponse,
)
from shared.http.compute import (
    ContainerResponse,
    MachineJoinCommandRequest,
)
from shared.http.compute_policy import WorkspaceComputePolicyUpdateRequest
from shared.http.gateway import (
    AttachToContainerResponse,
    CheckpointContainerRequest,
)

from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.components.theme import state_style, styled
from lazycloud.cli.control import (
    compute_client,
    gateway_client,
    resource_client,
    task_client,
)
from lazycloud.cli.pool_join import agent_join_interrupted, build_pool_join_command
from lazycloud.cli.task_results import task_result_human_value

task_app = typer.Typer(help="Inspect and manage tasks.")
container_app = typer.Typer(help="Inspect and manage containers.")
machine_app = typer.Typer(help="Manage self-hosted machines.")
cloud_app = typer.Typer(help="Connect and manage this account's cloud connection.")
cloud_connect_app = typer.Typer(help="Connect a cloud provider account.")
cloud_app.add_typer(cloud_connect_app, name="connect")
cloud_compute_app = typer.Typer(
    help="Inspect and update how capacity is provisioned in the connected account."
)
cloud_app.add_typer(cloud_compute_app, name="compute")
compute_app = typer.Typer(help="Inspect workspace compute pools and capacity.")
compute_policy_app = typer.Typer(help="Inspect and update workspace scheduling defaults.")
compute_app.add_typer(compute_policy_app, name="policy")


@compute_app.command("status")
def compute_status(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).summary()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    connection = response.connection
    rows: list[list[Any]] = [
        ["default pool", response.policy.default_pool],
        ["AWS account", connection.account_id if connection is not None else "not connected"],
        ["instances", str(response.instances.total)],
        ["ready", str(response.instances.ready)],
        ["pending", str(response.instances.pending)],
        ["workloads", str(response.workload_count)],
    ]
    console.print(table("Compute", ["field", "value"], rows))


@compute_app.command("instances")
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
            item.id,
            item.provider,
            item.region,
            item.instance_type or "",
            item.booted_template_version,
            item.service_state.value,
            (
                item.bootstrap_failure_reason.value
                if item.bootstrap_failure_reason is not None
                else ""
            ),
            item.bootstrap_failure_detail,
        ]
        for item in response.data
    ]
    console.print(
        table(
            "Compute instances",
            ["id", "provider", "region", "type", "template version", "state", "reason", "detail"],
            rows,
        )
    )


@compute_app.command("pools")
def compute_units(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """List the machine pools this workspace can run workloads in."""
    response = compute_client(workspace=workspace).pools()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [
        [
            item.name,
            "yes" if item.is_default else "",
            ", ".join(item.providers),
            str(item.unit_count),
            ", ".join(item.gpu_types),
        ]
        for item in response.data
    ]
    console.print(table("Compute pools", ["name", "default", "providers", "units", "gpus"], rows))


@compute_app.command("workloads")
def compute_workloads(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).workloads()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [[item.name, item.kind.value, str(item.pool)] for item in response.data]
    console.print(table("Compute workloads", ["name", "kind", "pool"], rows))


@compute_policy_app.command("show")
def compute_policy_show(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).policy()
    print_payload(ctx, response.model_dump(mode="json"))


@compute_policy_app.command("update")
def compute_policy_update(
    ctx: typer.Context,
    default_pool: Annotated[str, typer.Option("--default-pool")],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Set the pool this workspace's workloads land in when they name none."""
    client = compute_client(workspace=workspace)
    current = client.policy()
    response = client.update_policy(
        WorkspaceComputePolicyUpdateRequest(
            expected_revision=current.revision,
            default_pool=default_pool,
        )
    )
    print_payload(ctx, response.model_dump(mode="json"))


@cloud_compute_app.command("show")
def cloud_compute_show(ctx: typer.Context) -> None:
    """Show how capacity is provisioned in the connected account."""
    connection = compute_client().current_connection()
    if connection is None:
        raise typer.BadParameter("no cloud account is connected")
    print_payload(ctx, connection.compute.model_dump(mode="json"))


@cloud_compute_app.command("update")
def cloud_compute_update(
    ctx: typer.Context,
    default_region: Annotated[str | None, typer.Option("--default-region")] = None,
    default_instance_type: Annotated[
        str | None,
        typer.Option("--default-instance-type"),
    ] = None,
    initial_cpu_workers: Annotated[
        int | None,
        typer.Option("--initial-cpu-workers", min=0, max=100),
    ] = None,
    min_cpu_workers: Annotated[
        int | None,
        typer.Option("--min-cpu-workers", min=0, max=100),
    ] = None,
    max_cpu_instances: Annotated[int | None, typer.Option("--max-cpu", min=0, max=100)] = None,
    max_gpu_instances: Annotated[int | None, typer.Option("--max-gpu", min=0, max=100)] = None,
    min_free_cpu_millicores: Annotated[
        int | None,
        typer.Option("--min-free-cpu-millicores", min=0),
    ] = None,
    min_free_memory_mib: Annotated[
        int | None,
        typer.Option("--min-free-memory-mib", min=0),
    ] = None,
    allowed_regions: Annotated[list[str] | None, typer.Option("--allowed-region")] = None,
    allowed_instance_types: Annotated[
        list[str] | None,
        typer.Option("--allowed-instance-type"),
    ] = None,
    idle_timeout_seconds: Annotated[
        int | None,
        typer.Option("--idle-timeout", min=60, max=86_400),
    ] = None,
    root_volume_gib: Annotated[
        int | None,
        typer.Option("--root-volume-gib", min=50, max=2048),
    ] = None,
) -> None:
    """Change the connected account's provisioning limits and defaults."""
    client = compute_client()
    connection = client.current_connection()
    if connection is None:
        raise typer.BadParameter("no cloud account is connected")
    current = connection.compute
    # An option the caller left out keeps the value the account already carries, so
    # only the ones actually supplied are sent. Naming each field twice was a field
    # that silently reset itself the next time one was added.
    supplied: dict[str, str | int | tuple[str, ...] | None] = {
        "default_region": default_region,
        "default_instance_type": default_instance_type,
        "initial_cpu_workers": initial_cpu_workers,
        "min_cpu_workers": min_cpu_workers,
        "max_cpu_instances": max_cpu_instances,
        "max_gpu_instances": max_gpu_instances,
        "min_free_cpu_millicores": min_free_cpu_millicores,
        "min_free_memory_mib": min_free_memory_mib,
        "allowed_regions": None if allowed_regions is None else tuple(allowed_regions),
        "allowed_instance_types": (
            None if allowed_instance_types is None else tuple(allowed_instance_types)
        ),
        "idle_timeout_seconds": idle_timeout_seconds,
        "root_volume_gib": root_volume_gib,
    }
    response = client.update_compute_configuration(
        AwsComputeConfigurationUpdateRequest(
            expected_revision=current.revision,
            compute=current.model_copy(
                update={key: value for key, value in supplied.items() if value is not None}
            ),
        )
    )
    print_payload(ctx, response.compute.model_dump(mode="json"))


def _account_network(
    *,
    vpc_id: str | None,
    subnet_ids: tuple[str, ...],
    security_group_id: str | None,
) -> AwsAccountNetwork | None:
    """Build the network from options, or refuse a half-supplied one.

    All three or none: a connection carrying part of a network fails later, when
    a pool is requested, with an error about the pool rather than about the
    option that was left out.
    """
    supplied = [bool(vpc_id), bool(subnet_ids), bool(security_group_id)]
    if not any(supplied):
        return None
    if not all(supplied):
        raise typer.BadParameter(
            "give --network-vpc-id, two --network-subnet-id and "
            "--network-security-group-id together, or none of them"
        )
    if len(subnet_ids) != 2:
        raise typer.BadParameter(
            f"exactly two --network-subnet-id are required, in different "
            f"availability zones; got {len(subnet_ids)}"
        )
    return AwsAccountNetwork(
        vpc_id=vpc_id or "",
        subnet_ids=(subnet_ids[0], subnet_ids[1]),
        security_group_id=security_group_id or "",
    )


@cloud_connect_app.command("aws")
def cloud_connect_aws(
    ctx: typer.Context,
    account_id: Annotated[str, typer.Option("--account-id", help="AWS account ID.")],
    role_arn: Annotated[
        str | None,
        typer.Option("--role-arn", help="Existing cross-account management role."),
    ] = None,
    network_vpc_id: Annotated[
        str | None,
        typer.Option("--network-vpc-id", help="VPC managed pools launch into."),
    ] = None,
    network_subnet_id: Annotated[
        list[str] | None,
        typer.Option(
            "--network-subnet-id",
            help="Subnet to launch into. Give exactly two, in different zones.",
        ),
    ] = None,
    network_security_group_id: Annotated[
        str | None,
        typer.Option("--network-security-group-id", help="Security group nodes join."),
    ] = None,
    open_console: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open AWS authorization in a browser."),
    ] = False,
) -> None:
    """Connect an AWS account, which backs every workspace you own.

    The network options apply only with `--role-arn`. A managed-stack connection
    is given its network by the stack it deploys; an existing role is given one
    here, because nothing in that mode creates it.
    """
    network = _account_network(
        vpc_id=network_vpc_id,
        subnet_ids=tuple(network_subnet_id or ()),
        security_group_id=network_security_group_id,
    )
    response = compute_client().connect_account(
        account_id=account_id,
        role_arn=role_arn,
        network=network,
    )
    if open_console and response.authorization.url is not None:
        webbrowser.open(response.authorization.url)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(f"AWS account {account_id} is awaiting authorization.")
    if response.authorization.url is not None:
        console.print(response.authorization.url, highlight=False)
        console.print("Complete the AWS action, then run `cloud validate`.")
        return
    console.print("Configure the role trust with this external ID, then run `cloud validate`:")
    if response.authorization.external_id is None:
        raise RuntimeError("existing-role authorization did not return its external ID")
    console.print(response.authorization.external_id, highlight=False)


@cloud_app.command("reconnect")
def cloud_reconnect(
    ctx: typer.Context,
    role_arn: Annotated[
        str | None,
        typer.Option("--role-arn", help="Connected enterprise role to revalidate."),
    ] = None,
    open_console: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open AWS authorization in a browser."),
    ] = False,
) -> None:
    """Start replacement authorization for the connected account."""
    response = compute_client().reconnect_account(role_arn=role_arn)
    account_id = response.connection.account_id
    if open_console and response.authorization.url is not None:
        webbrowser.open(response.authorization.url)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(f"AWS account {account_id} replacement authorization is pending.")
    if response.authorization.url is not None:
        console.print(response.authorization.url, highlight=False)
        console.print("Complete the AWS action, then run `cloud validate`.")
    elif response.authorization.external_id is not None:
        console.print("Keep this external ID in the connected role trust:")
        console.print(response.authorization.external_id, highlight=False)


@cloud_app.command("validate")
def cloud_validate(
    ctx: typer.Context,
) -> None:
    """Validate the pending or active cloud authorization."""
    response = compute_client().validate_connection()
    account_id = response.account_id
    failure = _aws_validation_failure(response)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
    else:
        console.print(
            f"AWS account {account_id}:",
            styled(response.phase.value, state_style(response.phase)),
        )
        if failure is None:
            console.print("AWS authorization validated.")
        else:
            error_code, message = failure
            console.print(f"Validation failed ({error_code}): {message}")
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
        if json_output_enabled(ctx):
            print_payload(ctx, {"connection": None})
        else:
            console.print("Cloud connection: not connected")
        return
    account_id = response.account_id
    if watch:
        target_phase = until
        if target_phase is None:
            raise typer.BadParameter("--watch requires --until")
        deadline = time.monotonic() + timeout_seconds
        observed_phase: AwsAccountConnectionPhase | None = None
        while response.phase is not target_phase:
            if not json_output_enabled(ctx) and response.phase is not observed_phase:
                console.print(
                    f"{account_id}:",
                    styled(response.phase.value, state_style(response.phase)),
                )
                observed_phase = response.phase
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"timed out waiting for AWS account {account_id} to reach {target_phase.value}"
                )
            time.sleep(interval_seconds)
            current = client.current_connection()
            if current is None:
                raise RuntimeError("the AWS account connection was removed while waiting")
            response = current
    print_payload(ctx, response.model_dump(mode="json"))


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
        observed_phase: AwsAccountConnectionPhase | None = None
        while (
            connection is not None
            and connection.phase is not AwsAccountConnectionPhase.ActionRequired
        ):
            if not json_output_enabled(ctx) and connection.phase is not observed_phase:
                console.print(
                    f"AWS account {account_id}:",
                    styled(connection.phase.value, state_style(connection.phase)),
                )
                observed_phase = connection.phase
            if time.monotonic() >= deadline:
                raise RuntimeError(f"timed out waiting for AWS account {account_id} removal")
            time.sleep(interval_seconds)
            connection = client.current_connection()
    if json_output_enabled(ctx):
        print_payload(
            ctx,
            {
                "connection": (
                    connection.model_dump(mode="json") if connection is not None else None
                )
            },
        )
        return
    if connection is None:
        console.print(f"AWS account {account_id} removed.")
        return
    console.print(
        f"AWS account {account_id}:",
        styled(connection.phase.value, state_style(connection.phase)),
    )
    console.print(connection.detail)
    if connection.customer_action is not None:
        console.print(connection.customer_action.label)
        if connection.customer_action.url is not None:
            console.print(connection.customer_action.url, highlight=False)
    if connection.phase is AwsAccountConnectionPhase.ActionRequired:
        if (
            open_console
            and connection.customer_action is not None
            and connection.customer_action.url is not None
        ):
            webbrowser.open(connection.customer_action.url)
        return
    if not wait:
        console.print("Run `cloud disconnect --wait` to follow removal.")


@cloud_app.command("cancel-reconnect")
def cloud_cancel_reconnect(
    ctx: typer.Context,
) -> None:
    """Cancel a pending replacement authorization."""
    response = compute_client().cancel_reconnect()
    print_payload(ctx, response.model_dump(mode="json"))


@cloud_app.command("retry")
def cloud_retry(
    ctx: typer.Context,
) -> None:
    """Retry the connection's current pending action."""
    response = compute_client().retry_connection()
    print_payload(ctx, response.model_dump(mode="json"))


def _aws_validation_failure(response: AwsConnectionResponse) -> tuple[str, str] | None:
    for authorization in (response.pending_authorization, response.active_authorization):
        if authorization is not None and authorization.error_code is not None:
            return authorization.error_code.value, authorization.error_message or response.detail
    return None


@task_app.command("list")
def task_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1)] = 100,
    app_id: Annotated[str | None, typer.Option("--app-id")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).list_tasks(limit=limit, app_id=app_id)
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in response.data])
        return
    rows: list[list[str]] = [
        [
            item.id,
            item.status.value,
            item.container_id or "",
            item.workload.name if item.workload is not None else "",
            item.workspace_id or "",
        ]
        for item in response.data
    ]
    console.print(table("Tasks", ["id", "status", "container", "stub", "workspace"], rows))


@task_app.command("stop")
def task_stop(
    ctx: typer.Context,
    task_ids: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).stop_tasks(task_ids)
    print_payload(ctx, response.model_dump(mode="json"))


@task_app.command("show")
def task_show(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    task = task_client(workspace=workspace).detail(task_id)
    if json_output_enabled(ctx):
        print_payload(ctx, task.model_dump(mode="json"))
        return
    rows = [
        ["id", task.id],
        ["name", task.name],
        ["status", task.status.value],
        ["attempt", f"{task.attempt_number}/{task.max_attempts}"],
        ["created", task.created_at.isoformat()],
        ["started", task.started_at.isoformat() if task.started_at else ""],
        ["finished", task.finished_at.isoformat() if task.finished_at else ""],
        ["error", task.error or ""],
    ]
    console.print(table("Task", ["field", "value"], rows))


@task_app.command("result")
def task_result(
    ctx: typer.Context,
    task_id: str,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    timeout_seconds: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = task_client(workspace=workspace)
    result = client.handle(task_id).result(
        wait=wait,
        timeout_seconds=timeout_seconds,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, client.detail(task_id).model_dump(mode="json"))
        return
    if result.ok:
        print_payload(ctx, task_result_human_value(client.detail(task_id)))
        return
    console.print(result.error or f"task {task_id} finished with status {result.status.value}")


@task_app.command("logs")
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
        console.print("No logs found.")
        return
    for entry in logs:
        console.print(
            entry.message, highlight=False, end="" if entry.message.endswith("\n") else "\n"
        )


@task_app.command("cancel")
def task_cancel(
    ctx: typer.Context,
    task_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = task_client(workspace=workspace).cancel(task_id)
    print_payload(ctx, response.model_dump(mode="json"))


@container_app.command("list")
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
        [item.id, item.name, item.image, item.status.value, item.exit_code or ""]
        for item in containers
    ]
    console.print(table("Containers", ["id", "name", "image", "status", "exit"], rows))


@container_app.command("attach")
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
                console.print(response.output, highlight=False, end="")
        if response.done:
            terminal = response
            break
    if terminal is None:
        raise typer.BadParameter("container attach stream ended before the container completed")
    terminal = terminal.model_copy(update={"output": "".join(chunks)})
    if json_output:
        print_payload(ctx, terminal.model_dump(mode="json"))
        return
    console.print(f"exit code: {terminal.exit_code}")


@container_app.command("checkpoint")
def container_checkpoint(
    ctx: typer.Context,
    container_id: str,
    checkpoint_id: Annotated[str | None, typer.Option("--checkpoint-id")] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = gateway_client(workspace=workspace).checkpoint_container(
        CheckpointContainerRequest(container_id=container_id, checkpoint_id=checkpoint_id)
    )
    print_payload(ctx, response.model_dump(mode="json"))


@container_app.command("stop")
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
    if json_output_enabled(ctx):
        print_payload(ctx, results)
        return
    for item in results:
        console.print(f"stopped container {item['container_id']}")


@machine_app.command("list")
def machine_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """List the machines this account has joined."""
    machines = resource_client(workspace=workspace).list_machines().machines
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in machines])
        return
    rows: list[list[str]] = [[item.id, item.status.value, item.gpu or ""] for item in machines]
    console.print(table("Machines", ["id", "status", "gpu"], rows))


@machine_app.command("join")
def machine_join(
    ctx: typer.Context,
    ttl: Annotated[str, typer.Option("--ttl", help="Join token lifetime.")] = "",
    pool: Annotated[
        str,
        typer.Option("--pool", help="Machine pool to join, created if new."),
    ] = "",
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
        bool | None,
        typer.Option(
            "--background/--foreground",
            help="Install the agent as a background service or run it in the foreground.",
        ),
    ] = None,
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
    print_only: Annotated[
        bool,
        typer.Option("--print-only", help="Only print the generated join command."),
    ] = False,
) -> None:
    """Join this machine to your account, in the pool you name.

    No workspace: the host belongs to the account that connected it and serves every
    workspace that account owns.
    """
    if gpu_ids and max_gpus:
        raise typer.BadParameter("--gpu-ids and --max-gpus cannot both be set")

    response = compute_client().machine_join_command(
        MachineJoinCommandRequest(
            ttl=ttl,
            pool=MachinePool(pool),
            gpu=list(gpu or []),
        )
    )
    command = build_pool_join_command(
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
    if json_output_enabled(ctx):
        payload = response.model_dump(mode="json")
        payload["command"] = command
        print_payload(ctx, payload)
        return
    if print_only:
        console.print(command)
        return
    try:
        exit_code = subprocess.call(command, shell=True)
    except KeyboardInterrupt:
        return
    if agent_join_interrupted(exit_code):
        return
    if exit_code:
        raise typer.Exit(exit_code)
    console.print("Agent is running.")


@machine_app.command("remove")
def machine_remove(
    ctx: typer.Context,
    machine_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Remove a machine this account joined."""
    compute_client(workspace=workspace).remove_machine(machine_id)
    if json_output_enabled(ctx):
        print_payload(ctx, {"machine_id": machine_id})
        return
    console.print(f"removed machine {machine_id}")

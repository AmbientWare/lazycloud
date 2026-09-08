from __future__ import annotations

import subprocess
import time
import webbrowser
from typing import Annotated, Any

import typer
from shared.aws_connections import (
    AwsAccountComputeConfiguration,
    AwsAccountConnectionPhase,
    AwsAccountNetwork,
)
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
from shared.tasks import is_terminal_task_status

from lazycloud.cli.apps import resolve_app_id
from lazycloud.cli.components.cards import empty_state, notice_card, result_card
from lazycloud.cli.components.errors import ClientError
from lazycloud.cli.components.formatting import duration, timestamp
from lazycloud.cli.components.output import (
    console,
    emit,
    json_default,
    json_output_enabled,
    print_payload,
    table,
    write_stream,
)
from lazycloud.cli.components.theme import state_style, styled
from lazycloud.cli.control import (
    compute_client,
    gateway_client,
    resource_client,
    task_client,
)
from lazycloud.cli.pool_join import agent_join_interrupted, build_pool_join_command
from lazycloud.cli.task_results import task_result_human_value
from lazycloud.clients.aws import create_connection_stack

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


@compute_app.command("status", help="Show workspace compute capacity and policy.")
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
            "Compute",
            {
                "default_pool": response.policy.default_pool,
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


@compute_app.command("workloads", help="List workload-to-pool assignments.")
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


@compute_policy_app.command("show", help="Show the workspace compute policy.")
def compute_policy_show(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).policy()
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=result_card(
            "Compute policy",
            {"default_pool": response.default_pool},
        ),
    )


@compute_policy_app.command("update", help="Update the workspace compute policy.")
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
    emit(
        ctx,
        payload=response.model_dump(mode="json"),
        view=notice_card(
            "Compute policy updated",
            f"New workloads will use pool {response.default_pool} by default.",
            tone="success",
        ),
    )


@cloud_compute_app.command("show", help="Show connected-account compute settings.")
def cloud_compute_show(ctx: typer.Context) -> None:
    """Show how capacity is provisioned in the connected account."""
    connection = compute_client().current_connection()
    if connection is None:
        raise typer.BadParameter("no cloud account is connected")
    emit(
        ctx,
        payload=connection.compute.model_dump(mode="json"),
        view=result_card(
            "Compute settings",
            json_default(_compute_configuration_summary(connection.compute)),
        ),
    )


@cloud_compute_app.command("update", help="Update connected-account compute settings.")
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
    max_cpu_instances: Annotated[int | None, typer.Option("--max-cpu", min=0)] = None,
    max_gpu_instances: Annotated[int | None, typer.Option("--max-gpu", min=0)] = None,
    unlimited_cpu: Annotated[
        bool,
        typer.Option("--unlimited-cpu", help="Remove the CPU instance ceiling."),
    ] = False,
    unlimited_gpu: Annotated[
        bool,
        typer.Option("--unlimited-gpu", help="Remove the GPU instance ceiling."),
    ] = False,
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
    if max_cpu_instances is not None and unlimited_cpu:
        raise typer.BadParameter("choose --max-cpu or --unlimited-cpu, not both")
    if max_gpu_instances is not None and unlimited_gpu:
        raise typer.BadParameter("choose --max-gpu or --unlimited-gpu, not both")
    current = connection.compute
    # An option the caller left out keeps the value the account already carries, so
    # only the ones actually supplied are sent. Naming each field twice was a field
    # that silently reset itself the next time one was added.
    supplied: dict[str, str | int | tuple[str, ...] | None] = {
        "default_region": default_region,
        "default_instance_type": default_instance_type,
        "initial_cpu_workers": initial_cpu_workers,
        "min_cpu_workers": min_cpu_workers,
        "min_free_cpu_millicores": min_free_cpu_millicores,
        "min_free_memory_mib": min_free_memory_mib,
        "allowed_regions": None if allowed_regions is None else tuple(allowed_regions),
        "allowed_instance_types": (
            None if allowed_instance_types is None else tuple(allowed_instance_types)
        ),
        "idle_timeout_seconds": idle_timeout_seconds,
        "root_volume_gib": root_volume_gib,
    }
    updates: dict[str, str | int | tuple[str, ...] | None] = {
        key: value for key, value in supplied.items() if value is not None
    }
    if max_cpu_instances is not None or unlimited_cpu:
        updates["max_cpu_instances"] = max_cpu_instances
    if max_gpu_instances is not None or unlimited_gpu:
        updates["max_gpu_instances"] = max_gpu_instances
    changed_keys = tuple(updates)
    if not changed_keys:
        raise typer.BadParameter("provide at least one compute setting to update")
    response = client.update_compute_configuration(
        AwsComputeConfigurationUpdateRequest(
            expected_revision=current.revision,
            compute=current.model_copy(update=updates),
        )
    )
    emit(
        ctx,
        payload=response.compute.model_dump(mode="json"),
        view=result_card(
            "Compute settings updated",
            json_default(
                _compute_configuration_updates(
                    response.compute,
                    keys=changed_keys,
                )
            ),
            tone="success",
        ),
    )


def _compute_configuration_summary(
    configuration: AwsAccountComputeConfiguration,
) -> dict[str, object]:
    allowed_types: object = configuration.allowed_instance_types or "any"
    cpu_max: object = (
        configuration.max_cpu_instances
        if configuration.max_cpu_instances is not None
        else "unlimited"
    )
    return {
        "region": configuration.default_region,
        "instance_type": configuration.default_instance_type,
        "cpu_workers": (
            f"{configuration.min_cpu_workers} min, "
            f"{configuration.initial_cpu_workers} initial, "
            f"{cpu_max} max"
        ),
        "max_gpu_instances": (
            configuration.max_gpu_instances
            if configuration.max_gpu_instances is not None
            else "unlimited"
        ),
        "free_capacity": (
            f"{configuration.min_free_cpu_millicores / 1000:g} CPU, "
            f"{configuration.min_free_memory_mib} MiB memory"
        ),
        "allowed_regions": configuration.allowed_regions,
        "allowed_instance_types": allowed_types,
        "idle_timeout": duration(configuration.idle_timeout_seconds),
        "root_volume": f"{configuration.root_volume_gib} GiB",
    }


def _compute_configuration_updates(
    configuration: AwsAccountComputeConfiguration,
    *,
    keys: tuple[str, ...],
) -> dict[str, object]:
    values = configuration.model_dump(mode="python")
    updates: dict[str, object] = {}
    for key in keys:
        value = values[key]
        if key == "min_free_cpu_millicores":
            value = f"{configuration.min_free_cpu_millicores / 1000:g} CPU"
        elif key == "min_free_memory_mib":
            value = f"{configuration.min_free_memory_mib} MiB"
        elif key == "idle_timeout_seconds":
            value = duration(configuration.idle_timeout_seconds)
        elif key == "root_volume_gib":
            value = f"{configuration.root_volume_gib} GiB"
        elif key == "allowed_instance_types" and not value:
            value = "any"
        elif key in {"max_cpu_instances", "max_gpu_instances"} and value is None:
            value = "unlimited"
        updates[key] = value
    return updates


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
    max_cpu_instances: Annotated[
        int | None,
        typer.Option("--max-cpu", min=1, help="Optional CPU instance ceiling."),
    ] = None,
    max_gpu_instances: Annotated[
        int | None,
        typer.Option("--max-gpu", min=0, help="Optional GPU instance ceiling."),
    ] = None,
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
        max_cpu_instances=max_cpu_instances,
        max_gpu_instances=max_gpu_instances,
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
            "AWS authorization required",
            json_default(authorization),
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
            "Replacement authorization required",
            json_default(authorization),
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
            "AWS connection stack submitted",
            "Run `lazycloud cloud validate` after the stack finishes in CloudFormation.",
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
            "AWS authorization validated" if failure is None else "AWS validation failed",
            json_default(summary),
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
                "Cloud connection",
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
        view=result_card("Cloud connection", json_default(_connection_summary(response))),
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
            "AWS account removed" if connection is None else "AWS disconnect status",
            json_default(summary),
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
            "Reconnect cancelled",
            json_default(_connection_summary(response)),
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
            "Cloud action retried",
            json_default(_connection_summary(response)),
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
            "Tasks stopped",
            json_default(summary),
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
        view=result_card("Task", json_default(summary)),
    )


@task_app.command("result", help="Wait for and display a task result.")
def task_result(
    ctx: typer.Context,
    task_id: str,
    wait: Annotated[bool, typer.Option("--wait/--no-wait")] = True,
    timeout_seconds: Annotated[float | None, typer.Option("--timeout", min=0)] = None,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
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
        emit(
            ctx,
            payload=task.model_dump(mode="json"),
            view=result_card(
                "Task result",
                json_default(task_result_human_value(task)),
                tone="success",
            ),
        )
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
            "Task pending",
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
        console.print(empty_state("Task logs", "No log entries found."))
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
            "Task cancelled",
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
            "Container finished",
            {"exit_code": terminal.exit_code},
            tone="success" if terminal.exit_code == 0 else "warning",
        ),
    )
    if terminal.exit_code:
        raise typer.Exit(terminal.exit_code)


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
            "Checkpoint created",
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
            "Containers stopped",
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
        [str(item.pool), item.status.value, item.gpu or "", item.id] for item in machines
    ]
    console.print(table("Machines", ["pool", "status", "gpu", "id"], rows))


@machine_app.command("join", help="Join this machine to a compute pool.")
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
        console.print(
            notice_card(
                "Machine join command",
                command,
                hint="This command contains a short-lived credential. Do not share it.",
                tone="warning",
            )
        )
        return
    try:
        exit_code = subprocess.call(command, shell=True)
    except KeyboardInterrupt:
        return
    if agent_join_interrupted(exit_code):
        return
    if exit_code:
        raise typer.Exit(exit_code)
    emit(
        ctx,
        payload={"status": "running"},
        view=notice_card("Machine joined", "The agent is running.", tone="success"),
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
        view=notice_card("Machine removed", f"Removed {machine_id}.", tone="success"),
    )

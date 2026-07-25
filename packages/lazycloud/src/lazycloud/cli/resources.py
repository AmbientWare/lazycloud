from __future__ import annotations

import subprocess
import time
import webbrowser
from typing import Annotated, Any

import typer
from shared.aws_connections import AwsAccountConnectionPhase
from shared.compute_policy import AwsWorkspaceComputePolicy, ComputePlacementTarget
from shared.http.aws_connections import AwsConnectionResponse
from shared.http.compute import (
    ContainerResponse,
    MachineJoinCommandRequest,
    PoolCapacityExtendRequest,
    PoolCapacityLaunchRequest,
    PoolCreateRequest,
    PoolJoinCommandRequest,
    PoolJoinTokenRequest,
    PoolOfferQuery,
    PoolResponse,
    PoolScaleRequest,
    PoolScaleResponse,
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
pool_app = typer.Typer(help="Manage workspace compute pools and capacity.")
cloud_app = typer.Typer(help="Connect and manage the workspace's cloud connection.")
cloud_connect_app = typer.Typer(help="Connect a cloud provider account.")
cloud_app.add_typer(cloud_connect_app, name="connect")
compute_app = typer.Typer(help="Inspect workspace compute placement and capacity.")
compute_policy_app = typer.Typer(help="Inspect and update workspace compute guardrails.")
compute_app.add_typer(compute_policy_app, name="policy")


def _pool_offer_query(
    *,
    provider: list[str] | None,
    region: list[str] | None,
    gpu: list[str] | None,
    nodes: int,
    ttl: str,
    max_spend: float,
    min_reliability: float,
    offer_id: str,
) -> PoolOfferQuery:
    return PoolOfferQuery(
        provider=list(provider or []),
        region=list(region or []),
        gpu=list(gpu or []),
        node_count=nodes,
        ttl=ttl,
        max_spend=max_spend,
        min_reliability=min_reliability,
        offer_id=offer_id,
    )


def _pool_rows(pools: list[PoolResponse]) -> list[list[str]]:
    return [
        [
            pool.name,
            pool.provider,
            str(pool.priority),
            f"{pool.min_workers}/{pool.initial_workers}/{pool.max_workers}",
            ", ".join(
                (
                    "scaling enabled" if pool.scaling_enabled else "scaling disabled",
                    "default eligible" if pool.default_eligible else "explicit only",
                )
            ),
            "; ".join(
                (
                    f"{pool.worker_cpu_millicores}m CPU",
                    f"{pool.worker_memory_mib} MiB",
                    (
                        f"{pool.worker_gpu_count}x {pool.worker_gpu_type}"
                        if pool.worker_gpu_count
                        else "no GPU"
                    ),
                    f"runtimes {', '.join(pool.worker_runtimes)}",
                    "preemptible" if pool.worker_preemptible else "on-demand",
                )
            ),
            (
                f"free {pool.min_free_cpu_millicores}m CPU / "
                f"{pool.min_free_memory_mib} MiB / {pool.min_free_gpu_count} GPU; "
                f"drain {pool.idle_drain_timeout_seconds}s; "
                f"cooldown {pool.scale_up_cooldown_seconds}s up / "
                f"{pool.scale_down_cooldown_seconds}s down; "
                f"registration {pool.registration_timeout_seconds}s"
            ),
        ]
        for pool in pools
    ]


def _print_pools(pools: list[PoolResponse], *, title: str) -> None:
    console.print(
        table(
            title,
            [
                "name",
                "provider",
                "priority",
                "workers min/initial/max",
                "placement",
                "worker shape",
                "timeouts",
            ],
            _pool_rows(pools),
        )
    )


@pool_app.command("list", help="List durable workspace compute pools and their capacity policy.")
def pool_list(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).list_pools()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    _print_pools(response.pools, title="Compute Pools")


@pool_app.command("create", help="Create a workspace compute pool with an exact capacity policy.")
def pool_create(
    ctx: typer.Context,
    name: str,
    provider: Annotated[str, typer.Option("--provider")] = "agent",
    initial_workers: Annotated[int, typer.Option("--initial-workers", min=0)] = 0,
    min_workers: Annotated[int, typer.Option("--min-workers", min=0)] = 0,
    max_workers: Annotated[int, typer.Option("--max-workers", min=0)] = 1,
    scaling_enabled: Annotated[
        bool,
        typer.Option("--scaling-enabled/--scaling-disabled"),
    ] = False,
    default_eligible: Annotated[
        bool,
        typer.Option("--default-eligible/--no-default-eligible"),
    ] = False,
    priority: Annotated[int, typer.Option("--priority")] = 0,
    min_free_cpu_millicores: Annotated[
        int,
        typer.Option("--min-free-cpu-millicores", min=0),
    ] = 0,
    min_free_memory_mib: Annotated[
        int,
        typer.Option("--min-free-memory-mib", min=0),
    ] = 0,
    min_free_gpu_count: Annotated[
        int,
        typer.Option("--min-free-gpu-count", min=0),
    ] = 0,
    worker_cpu_millicores: Annotated[
        int,
        typer.Option("--worker-cpu-millicores", min=0),
    ] = 0,
    worker_memory_mib: Annotated[
        int,
        typer.Option("--worker-memory-mib", min=0),
    ] = 0,
    worker_gpu_type: Annotated[str, typer.Option("--worker-gpu-type")] = "",
    worker_gpu_count: Annotated[int, typer.Option("--worker-gpu-count", min=0)] = 0,
    worker_runtimes: Annotated[
        list[str] | None,
        typer.Option("--worker-runtime"),
    ] = None,
    worker_preemptible: Annotated[
        bool,
        typer.Option("--worker-preemptible/--no-worker-preemptible"),
    ] = False,
    idle_drain_timeout_seconds: Annotated[
        int,
        typer.Option("--idle-drain-timeout-seconds", min=60, max=86_400),
    ] = 300,
    scale_up_cooldown_seconds: Annotated[
        int,
        typer.Option("--scale-up-cooldown-seconds", min=0, max=86_400),
    ] = 5,
    scale_down_cooldown_seconds: Annotated[
        int,
        typer.Option("--scale-down-cooldown-seconds", min=0, max=86_400),
    ] = 60,
    registration_timeout_seconds: Annotated[
        int,
        typer.Option("--registration-timeout-seconds", min=30, max=3_600),
    ] = 600,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = resource_client(workspace=workspace).create_pool(
        PoolCreateRequest(
            name=name,
            provider=provider,
            initial_workers=initial_workers,
            min_workers=min_workers,
            max_workers=max_workers,
            scaling_enabled=scaling_enabled,
            default_eligible=default_eligible,
            priority=priority,
            min_free_cpu_millicores=min_free_cpu_millicores,
            min_free_memory_mib=min_free_memory_mib,
            min_free_gpu_count=min_free_gpu_count,
            worker_cpu_millicores=worker_cpu_millicores,
            worker_memory_mib=worker_memory_mib,
            worker_gpu_type=worker_gpu_type,
            worker_gpu_count=worker_gpu_count,
            worker_runtimes=tuple(worker_runtimes or ("runc",)),
            worker_preemptible=worker_preemptible,
            idle_drain_timeout_seconds=idle_drain_timeout_seconds,
            scale_up_cooldown_seconds=scale_up_cooldown_seconds,
            scale_down_cooldown_seconds=scale_down_cooldown_seconds,
            registration_timeout_seconds=registration_timeout_seconds,
        )
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    _print_pools([response], title="Compute Pool Created")


@pool_app.command("delete", help="Delete a workspace compute pool.")
def pool_delete(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    resource_client(workspace=workspace).delete_pool(name)
    print_payload(ctx, {"name": name, "deleted": True})


@pool_app.command("offers")
def pool_offers(
    ctx: typer.Context,
    name: str,
    provider: Annotated[list[str] | None, typer.Option("--provider")] = None,
    region: Annotated[list[str] | None, typer.Option("--region")] = None,
    gpu: Annotated[list[str] | None, typer.Option("--gpu")] = None,
    nodes: Annotated[int, typer.Option("--nodes", min=1)] = 1,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
    max_spend: Annotated[float, typer.Option("--max-spend", min=0.0)] = 0.0,
    min_reliability: Annotated[float, typer.Option("--min-reliability", min=0.0, max=1.0)] = 0.0,
    offer_id: Annotated[str, typer.Option("--offer-id")] = "",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).list_pool_offers(
        name,
        _pool_offer_query(
            provider=provider,
            region=region,
            gpu=gpu,
            nodes=nodes,
            ttl=ttl,
            max_spend=max_spend,
            min_reliability=min_reliability,
            offer_id=offer_id,
        ),
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows: list[list[str]] = [
        [
            item.id,
            item.provider,
            item.region,
            item.instance_type,
            item.gpu,
            str(item.hourly_cost_micros),
        ]
        for item in response.data
    ]
    console.print(
        table(
            f"Pool Offers: {name}",
            ["id", "provider", "region", "type", "gpu", "hourly µ$"],
            rows,
        )
    )


@pool_app.command("launch")
def pool_launch(
    ctx: typer.Context,
    name: str,
    provider: Annotated[list[str] | None, typer.Option("--provider")] = None,
    region: Annotated[list[str] | None, typer.Option("--region")] = None,
    gpu: Annotated[list[str] | None, typer.Option("--gpu")] = None,
    nodes: Annotated[int, typer.Option("--nodes", min=1)] = 1,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
    max_spend: Annotated[float, typer.Option("--max-spend", min=0.0)] = 0.0,
    min_reliability: Annotated[float, typer.Option("--min-reliability", min=0.0, max=1.0)] = 0.0,
    offer_id: Annotated[str, typer.Option("--offer-id")] = "",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    query = _pool_offer_query(
        provider=provider,
        region=region,
        gpu=gpu,
        nodes=nodes,
        ttl=ttl,
        max_spend=max_spend,
        min_reliability=min_reliability,
        offer_id=offer_id,
    )
    response = compute_client(workspace=workspace).launch_pool_capacity(
        name,
        PoolCapacityLaunchRequest.model_validate(query.model_dump(mode="json")),
    )
    print_payload(ctx, response.model_dump(mode="json"))


@pool_app.command("extend")
def pool_extend(
    ctx: typer.Context,
    name: str,
    ttl: Annotated[str, typer.Option("--ttl")],
    max_spend: Annotated[float, typer.Option("--max-spend", min=0.0)],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).extend_pool_capacity(
        name,
        PoolCapacityExtendRequest(ttl=ttl, max_spend=max_spend),
    )
    print_payload(ctx, response.model_dump(mode="json"))


@pool_app.command(
    "scale",
    help="Set a managed pool's durable desired node count.",
)
def pool_scale(
    ctx: typer.Context,
    name: str,
    nodes: Annotated[int, typer.Option("--nodes", min=0)],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).scale_pool(
        name,
        PoolScaleRequest(desired_machines=nodes),
    )
    _print_pool_state(ctx, response)


@pool_app.command(
    "status",
    help="Inspect a managed pool's durable and observed capacity state.",
)
def pool_status(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).get_pool_state(name)
    _print_pool_state(ctx, response)


def _print_pool_state(ctx: typer.Context, response: PoolScaleResponse) -> None:
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    detail = f"{response.phase.value}: {response.status}"
    if response.degraded_reason:
        detail = f"{detail}, degraded: {response.degraded_reason}"
    console.print(
        f"Pool {response.name}: desired {response.desired_machines} nodes, "
        f"observed {response.observed_machines}, maximum {response.max_machines} "
        f"({detail})"
    )


@pool_app.command("join-token")
def pool_join_token(
    ctx: typer.Context,
    name: str,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).create_pool_join_token(
        name,
        PoolJoinTokenRequest(ttl=ttl),
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(response.token)


@pool_app.command("revoke-join-token")
def pool_revoke_join_token(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    compute_client(workspace=workspace).revoke_pool_join_token(name)
    print_payload(ctx, {"pool_name": name, "revoked": True})


@pool_app.command("join-command")
def pool_join_command(
    ctx: typer.Context,
    name: str,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).pool_join_command(
        name,
        PoolJoinCommandRequest(ttl=ttl),
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(response.command)


@pool_app.command("machines")
def pool_machines(
    ctx: typer.Context,
    name: str,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    cursor: Annotated[str, typer.Option("--cursor")] = "",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).list_pool_machines(
        name,
        limit=limit,
        cursor=cursor,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows: list[list[str]] = [
        [item.id, item.status, item.provider_name, item.gpu, str(item.gpu_count)]
        for item in response.data
    ]
    console.print(
        table(
            f"Pool Machines: {name}",
            ["id", "status", "provider", "gpu", "gpus"],
            rows,
        )
    )


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
        ["default placement", response.policy.default_placement.value],
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
            item.bootstrap_phase.value,
            (
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
            ["id", "provider", "region", "type", "phase", "reason"],
            rows,
        )
    )


@compute_app.command("workloads")
def compute_workloads(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = compute_client(workspace=workspace).workloads()
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [
        [item.name, item.kind.value, item.placement.target.value, item.placement.region]
        for item in response.data
    ]
    console.print(table("Compute workloads", ["name", "kind", "placement", "region"], rows))


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
    default_placement: Annotated[
        ComputePlacementTarget | None,
        typer.Option("--default-placement"),
    ] = None,
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = compute_client(workspace=workspace)
    current = client.policy()
    aws = current.aws
    request = WorkspaceComputePolicyUpdateRequest(
        expected_revision=current.revision,
        default_placement=default_placement or current.default_placement,
        aws=AwsWorkspaceComputePolicy(
            default_region=default_region or aws.default_region,
            default_instance_type=(
                aws.default_instance_type
                if default_instance_type is None
                else default_instance_type
            ),
            initial_cpu_workers=(
                aws.initial_cpu_workers if initial_cpu_workers is None else initial_cpu_workers
            ),
            min_cpu_workers=(aws.min_cpu_workers if min_cpu_workers is None else min_cpu_workers),
            max_cpu_instances=(
                aws.max_cpu_instances if max_cpu_instances is None else max_cpu_instances
            ),
            max_gpu_instances=(
                aws.max_gpu_instances if max_gpu_instances is None else max_gpu_instances
            ),
            min_free_cpu_millicores=(
                aws.min_free_cpu_millicores
                if min_free_cpu_millicores is None
                else min_free_cpu_millicores
            ),
            min_free_memory_mib=(
                aws.min_free_memory_mib if min_free_memory_mib is None else min_free_memory_mib
            ),
            allowed_regions=(
                aws.allowed_regions if allowed_regions is None else tuple(allowed_regions)
            ),
            allowed_instance_types=(
                aws.allowed_instance_types
                if allowed_instance_types is None
                else tuple(allowed_instance_types)
            ),
            idle_timeout_seconds=(
                aws.idle_timeout_seconds if idle_timeout_seconds is None else idle_timeout_seconds
            ),
            root_volume_gib=aws.root_volume_gib if root_volume_gib is None else root_volume_gib,
        ),
    )
    response = client.update_policy(request)
    print_payload(ctx, response.model_dump(mode="json"))


@cloud_connect_app.command("aws")
def cloud_connect_aws(
    ctx: typer.Context,
    account_id: Annotated[str, typer.Option("--account-id", help="AWS account ID.")],
    role_arn: Annotated[
        str | None,
        typer.Option("--role-arn", help="Existing cross-account management role."),
    ] = None,
    open_console: Annotated[
        bool,
        typer.Option("--open/--no-open", help="Open AWS authorization in a browser."),
    ] = False,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Connect an AWS account to this workspace."""
    response = compute_client(workspace=workspace).connect_account(
        account_id=account_id,
        role_arn=role_arn,
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Start replacement authorization for the connected account."""
    response = compute_client(workspace=workspace).reconnect_account(role_arn=role_arn)
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Validate the pending or active cloud authorization."""
    response = compute_client(workspace=workspace).validate_connection()
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Show the workspace's cloud connection status."""
    if until is not None and not watch:
        raise typer.BadParameter("--until requires --watch")
    if watch and until is None:
        raise typer.BadParameter("--watch requires --until")
    client = compute_client(workspace=workspace)
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Disconnect the cloud account and remove its managed compute."""
    client = compute_client(workspace=workspace)
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Cancel a pending replacement authorization."""
    response = compute_client(workspace=workspace).cancel_reconnect()
    print_payload(ctx, response.model_dump(mode="json"))


@cloud_app.command("retry")
def cloud_retry(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Retry the connection's current pending action."""
    response = compute_client(workspace=workspace).retry_connection()
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
    """List this workspace's self-hosted machines."""
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
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    """Join this machine to the workspace's self-hosted compute."""
    if gpu_ids and max_gpus:
        raise typer.BadParameter("--gpu-ids and --max-gpus cannot both be set")

    response = compute_client(workspace=workspace).machine_join_command(
        MachineJoinCommandRequest(
            ttl=ttl,
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
    """Remove a machine from the workspace's self-hosted compute."""
    compute_client(workspace=workspace).remove_machine(machine_id)
    if json_output_enabled(ctx):
        print_payload(ctx, {"machine_id": machine_id})
        return
    console.print(f"removed machine {machine_id}")

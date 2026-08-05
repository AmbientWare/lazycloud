from __future__ import annotations

import subprocess
from typing import Annotated

import typer
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    command_from_args,
    console,
    json_output_enabled,
    print_events_table,
    print_payload,
    table,
)
from lazycloud.cli.control import compute_client, control_config
from lazycloud.cli.pool_join import agent_join_interrupted, build_pool_join_command
from lazycloud.cli.resources import container_attach, container_checkpoint
from lazycloud.clients.map.control import MapControlClient
from lazycloud.clients.simplequeue.control import SimpleQueueControlClient
from pydantic import JsonValue
from shared.http.collections import MAX_MAP_TTL_SECONDS
from shared.http.compute import (
    ContainerResponse,
    ContainerRunRequest,
    MachineCreateRequest,
    PoolCreateRequest,
    PoolJoinCommandRequest,
)
from shared.http.observability import EventHistoryRequest, LogQueryRequest

from cli.api_client import admin_api_client
from cli.components.terminal import print_stream_message

queue_app = typer.Typer(help="Manage queues.")
map_app = typer.Typer(help="Manage durable maps.")
worker_app = typer.Typer(help="Manage worker records.")
container_app = typer.Typer(help="Manage containers.")
pool_app = typer.Typer(help="Manage workspace compute pools and capacity.")


def _queue_client() -> SimpleQueueControlClient:
    config = control_config()
    return SimpleQueueControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


def _map_client() -> MapControlClient:
    config = control_config()
    return MapControlClient.from_endpoint(
        config.endpoint,
        token=config.token,
        timeout_seconds=config.timeout_seconds,
        workspace=config.workspace,
    )


@queue_app.command("list")
def queue_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_queues().queues
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    console.print(
        table(
            "Queues",
            ["name", "size", "oldest seconds", "puts/min"],
            [
                [
                    item.name,
                    str(item.size),
                    str(item.oldest_message_age_seconds or ""),
                    str(item.put_rate_per_minute),
                ]
                for item in records
            ],
        )
    )


@queue_app.command("put")
def queue_put(ctx: typer.Context, name: str, value: str) -> None:
    response = _queue_client().put(name, value.encode())
    print_payload(ctx, response.model_dump(mode="json"))


@queue_app.command("pop")
def queue_pop(ctx: typer.Context, name: str) -> None:
    response = _queue_client().pop(name)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(response.bytes_value().decode(errors="replace"))


@queue_app.command("peek")
def queue_peek(ctx: typer.Context, name: str) -> None:
    response = _queue_client().peek(name)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(response.bytes_value().decode(errors="replace"))


@queue_app.command("size")
def queue_size(ctx: typer.Context, name: str) -> None:
    print_payload(ctx, _queue_client().size(name).model_dump(mode="json"))


@queue_app.command("delete")
def queue_delete(name: str) -> None:
    _queue_client().delete(name)
    console.print(f"deleted queue {name}")


@map_app.command("list")
def map_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_maps().maps
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    console.print(
        table(
            "Maps",
            ["name", "keys", "bytes", "expiring"],
            [
                [item.name, str(item.count), str(item.size_bytes), str(item.expiring_keys)]
                for item in records
            ],
        )
    )


@map_app.command("set")
def map_set(
    ctx: typer.Context,
    name: str,
    key: str,
    value: str,
    ttl_seconds: Annotated[int, typer.Option("--ttl", min=0)] = MAX_MAP_TTL_SECONDS,
) -> None:
    response = _map_client().set(name, key, value.encode(), ttl_seconds=ttl_seconds)
    print_payload(ctx, response.model_dump(mode="json"))


@map_app.command("get")
def map_get(ctx: typer.Context, name: str, key: str) -> None:
    response = _map_client().get(name, key)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(response.bytes_value().decode(errors="replace"))


@map_app.command("keys")
def map_keys(ctx: typer.Context, name: str) -> None:
    print_payload(ctx, _map_client().keys(name).model_dump(mode="json"))


@map_app.command("delete-key")
def map_delete_key(name: str, key: str) -> None:
    _map_client().delete(name, key)
    console.print(f"deleted key {key} from map {name}")


@map_app.command("delete")
def map_delete(name: str) -> None:
    _map_client().delete_map(name)
    console.print(f"deleted map {name}")


def container_run(
    ctx: typer.Context,
    image: str,
    command: Annotated[list[str] | None, typer.Argument()] = None,
    name: Annotated[str, typer.Option("--name")] = "container",
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = admin_api_client(workspace).run_container(
        ContainerRunRequest(
            name=name,
            image=image,
            command=command_from_args(command or []),
        )
    )
    print_payload(ctx, response.model_dump(mode="json"))


def container_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = admin_api_client(workspace)
    containers: list[ContainerResponse] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while len(containers) < limit:
        page = client.list_containers(
            limit=min(100, limit - len(containers)),
            cursor=cursor,
        )
        containers.extend(item.container for item in page.data)
        if not page.next or page.next in seen_cursors:
            break
        seen_cursors.add(page.next)
        cursor = page.next
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in containers])
        return
    rows = [
        [item.id, item.name, item.image, item.status.value, str(item.exit_code or "")]
        for item in containers
    ]
    console.print(table("Containers", ["id", "name", "image", "status", "exit"], rows))


def container_show(
    ctx: typer.Context,
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = admin_api_client(workspace).get_container(container_id)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(
        table(
            "Container",
            ["field", "value"],
            [
                ["id", response.id],
                ["name", response.name],
                ["status", response.status.value],
                ["image", response.image],
                ["command", " ".join(response.command)],
                ["workspace", response.workspace_id],
                ["machine", response.machine_id or ""],
                ["worker", response.worker_id or ""],
            ],
        )
    )


def container_logs(
    ctx: typer.Context,
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    page: Annotated[int, typer.Option("--page", min=0)] = 0,
    query: Annotated[str | None, typer.Option("--query")] = None,
) -> None:
    selected = current_workspace(workspace)
    response = admin_api_client(selected).logs(
        LogQueryRequest(
            workspace_id=selected,
            container_id=container_id,
            limit=limit,
            page=page,
            query=query,
        )
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    if not response.data:
        console.print("No logs found.")
        return
    for entry in response.data:
        print_stream_message(entry.stream, entry.message, console=console)


def container_events(
    ctx: typer.Context,
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1000)] = 100,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
) -> None:
    selected = current_workspace(workspace)
    response = admin_api_client(selected).events(
        EventHistoryRequest(
            workspace_id=selected,
            container_id=container_id,
            limit=limit,
            cursor=cursor,
        )
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    print_events_table("Container Events", list(response.data))


def container_delete(
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    admin_api_client(workspace).delete_container(container_id)
    console.print(f"deleted container {container_id}")


def container_stop(
    ctx: typer.Context,
    container_ids: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = admin_api_client(workspace)
    stopped = [client.stop_container(container_id) for container_id in container_ids]
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in stopped])
        return
    for item in stopped:
        console.print(f"stopped container {item.id}")


def pool_create(
    ctx: typer.Context,
    name: str,
    machine_pool: Annotated[str, typer.Option("--pool")] = "",
    provider: Annotated[str, typer.Option("--provider")] = "agent",
    initial_machines: Annotated[int, typer.Option("--initial-machines", min=0)] = 0,
    min_machines: Annotated[int, typer.Option("--min-machines", min=0)] = 0,
    max_machines: Annotated[int, typer.Option("--max-machines", min=0)] = 1,
    scaling_enabled: Annotated[bool, typer.Option("--scaling-enabled")] = False,
    default_eligible: Annotated[bool, typer.Option("--default-eligible")] = False,
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
    worker_runtime: Annotated[list[str] | None, typer.Option("--worker-runtime")] = None,
    worker_preemptible: Annotated[bool, typer.Option("--worker-preemptible")] = False,
    idle_drain_timeout_seconds: Annotated[
        int,
        typer.Option("--idle-drain-timeout", min=60, max=86_400),
    ] = 300,
    registration_timeout_seconds: Annotated[
        int,
        typer.Option("--registration-timeout", min=30, max=3_600),
    ] = 600,
) -> None:
    response = admin_api_client().create_pool(
        PoolCreateRequest(
            name=name,
            machine_pool=machine_pool,
            provider=provider,
            initial_machines=initial_machines,
            min_machines=min_machines,
            max_machines=max_machines,
            scaling_enabled=scaling_enabled,
            default_eligible=default_eligible,
            worker_cpu_millicores=worker_cpu_millicores,
            worker_memory_mib=worker_memory_mib,
            worker_gpu_type=worker_gpu_type,
            worker_gpu_count=worker_gpu_count,
            worker_runtimes=tuple(worker_runtime or ("runc",)),
            worker_preemptible=worker_preemptible,
            idle_drain_timeout_seconds=idle_drain_timeout_seconds,
            registration_timeout_seconds=registration_timeout_seconds,
        )
    )
    print_payload(ctx, response.model_dump(mode="json"))


def pool_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_pools().pools
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    console.print(
        table(
            "Pools",
            ["name", "pool", "provider", "owner", "initial", "min", "max", "scaling"],
            [
                [
                    item.name,
                    item.machine_pool,
                    item.provider,
                    item.capacity_owner_id,
                    str(item.initial_machines),
                    str(item.min_machines),
                    str(item.max_machines),
                    str(item.scaling_enabled),
                ]
                for item in records
            ],
        )
    )


def pool_delete(name: str) -> None:
    admin_api_client().delete_pool(name)
    console.print(f"deleted pool {name}")


def pool_clear_degraded(
    ctx: typer.Context,
    name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = admin_api_client(workspace).clear_pool_degradation(name)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(
        f"Pool {response.name}: desired {response.desired_machines} nodes, "
        f"observed {response.observed_machines}, maximum {response.max_machines} "
        f"({response.phase.value}: {response.status})"
    )


def pool_join(
    ctx: typer.Context,
    name: str,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
    agent_bin: Annotated[str, typer.Option("--agent-bin")] = "",
    executor: Annotated[str, typer.Option("--executor")] = "",
    worker_image: Annotated[str, typer.Option("--worker-image")] = "",
    print_only: Annotated[bool, typer.Option("--print-only")] = False,
) -> None:
    response = admin_api_client().pool_join_command(name, PoolJoinCommandRequest(ttl=ttl))
    command = build_pool_join_command(
        response.command,
        agent_bin=agent_bin,
        executor=executor,
        worker_image=worker_image,
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


def machine_create(
    ctx: typer.Context,
    provider: Annotated[str, typer.Option("--provider")] = "local",
    cpu: Annotated[float | None, typer.Option("--cpu")] = None,
    memory: Annotated[str | None, typer.Option("--memory")] = None,
    gpu: Annotated[str | None, typer.Option("--gpu")] = None,
) -> None:
    response = admin_api_client().create_machine(
        MachineCreateRequest(
            provider=provider,
            cpu=cpu,
            memory=memory,
            gpu=gpu,
        )
    )
    print_payload(ctx, response.model_dump(mode="json"))


def machine_delete(machine_id: str) -> None:
    admin_api_client().delete_machine(machine_id)
    console.print(f"deleted machine {machine_id}")


def _machine_workers(machine_id: str, *, action: str) -> list[dict[str, JsonValue]]:
    client = admin_api_client()
    workers = [item for item in client.list_workers().workers if item.machine_id == machine_id]
    compute = compute_client()
    results: list[dict[str, JsonValue]] = []
    for worker in workers:
        if action == "cordon":
            response = compute.cordon_worker(worker.id)
        elif action == "uncordon":
            response = compute.uncordon_worker(worker.id)
        elif action == "drain":
            response = compute.drain_worker(worker.id)
        else:
            raise ValueError(f"unsupported machine action: {action}")
        results.append(response.model_dump(mode="json"))
    return results


def machine_cordon(ctx: typer.Context, machine_id: str) -> None:
    print_payload(ctx, _machine_workers(machine_id, action="cordon"))


def machine_uncordon(ctx: typer.Context, machine_id: str) -> None:
    print_payload(ctx, _machine_workers(machine_id, action="uncordon"))


def machine_drain(ctx: typer.Context, machine_id: str) -> None:
    print_payload(ctx, _machine_workers(machine_id, action="drain"))


@worker_app.command("list")
def worker_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_workers().workers
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    console.print(
        table(
            "Workers",
            ["id", "pool", "machine", "status", "free cpu", "free mem", "free gpu"],
            [
                [
                    item.id,
                    item.pool_name,
                    item.machine_id,
                    item.status,
                    str(item.free_cpu),
                    str(item.free_memory),
                    str(item.free_gpu_count),
                ]
                for item in records
            ],
        )
    )


@worker_app.command("delete")
def worker_delete(worker_id: str) -> None:
    admin_api_client().delete_worker(worker_id)
    console.print(f"deleted worker {worker_id}")


@worker_app.command("cordon")
def worker_cordon(ctx: typer.Context, worker_id: str) -> None:
    print_payload(ctx, compute_client().cordon_worker(worker_id).model_dump(mode="json"))


@worker_app.command("uncordon")
def worker_uncordon(ctx: typer.Context, worker_id: str) -> None:
    print_payload(ctx, compute_client().uncordon_worker(worker_id).model_dump(mode="json"))


@worker_app.command("drain")
def worker_drain(ctx: typer.Context, worker_id: str) -> None:
    print_payload(ctx, compute_client().drain_worker(worker_id).model_dump(mode="json"))


container_app.command("list")(container_list)
container_app.command(
    "run",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)(container_run)
container_app.command("show")(container_show)
container_app.command("logs")(container_logs)
container_app.command("events")(container_events)
container_app.command("stop")(container_stop)
container_app.command("delete")(container_delete)
container_app.command("attach")(container_attach)
container_app.command("checkpoint")(container_checkpoint)


pool_app.command("create")(pool_create)
pool_app.command("list")(pool_list)
pool_app.command("delete")(pool_delete)
pool_app.command("join")(pool_join)
pool_app.command(
    "clear-degraded",
    help="Let a pool that exhausted its relaunch attempts buy machines again.",
)(pool_clear_degraded)


def register_machine_extensions(group: typer.Typer) -> None:
    group.command("create")(machine_create)
    group.command("delete")(machine_delete)
    group.command("cordon")(machine_cordon)
    group.command("uncordon")(machine_uncordon)
    group.command("drain")(machine_drain)


__all__ = [
    "container_app",
    "map_app",
    "pool_app",
    "queue_app",
    "register_machine_extensions",
    "worker_app",
]

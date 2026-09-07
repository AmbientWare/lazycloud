from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from lazycloud.cli.components.cards import empty_state, notice_card
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.formatting import bytes_count, duration, timestamp
from lazycloud.cli.components.output import (
    command_from_args,
    console,
    json_output_enabled,
    print_events_table,
    print_payload,
    table,
    write_stream,
)
from lazycloud.cli.components.progress import print_stream_message
from lazycloud.cli.components.results import emit_notice, emit_result
from lazycloud.cli.control import compute_client, control_config
from lazycloud.cli.pool_join import agent_join_interrupted, build_pool_join_command
from lazycloud.cli.resources import container_attach, container_checkpoint
from lazycloud.clients.map.control import MapControlClient
from lazycloud.clients.simplequeue.control import SimpleQueueControlClient
from pydantic import JsonValue
from shared.compute_policy import MachinePool
from shared.container_requests import OciRuntimeName
from shared.http.collections import MAX_MAP_TTL_SECONDS
from shared.http.compute import (
    ContainerResponse,
    ContainerRunRequest,
    MachineCreateRequest,
    MachineJoinCommandRequest,
    UnitCreateRequest,
    UnitJoinCommandRequest,
    UnitJoinTokenRequest,
)
from shared.http.observability import EventHistoryRequest, LogQueryRequest

from cli.api_client import admin_api_client

queue_app = typer.Typer(help="Manage queues.")
map_app = typer.Typer(help="Manage durable maps.")
worker_app = typer.Typer(help="Manage worker records.")
container_app = typer.Typer(help="Manage containers.")
unit_app = typer.Typer(help="Manage workspace provisioning units and capacity.")


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
                    (
                        duration(item.oldest_message_age_seconds)
                        if item.oldest_message_age_seconds is not None
                        else "none"
                    ),
                    str(item.put_rate_per_minute),
                ]
                for item in records
            ],
        )
    )


@queue_app.command("put")
def queue_put(ctx: typer.Context, name: str, value: str) -> None:
    response = _queue_client().put(name, value.encode())
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Value queued",
        fields={"queue": name},
        tone="success",
    )


@queue_app.command("pop")
def queue_pop(ctx: typer.Context, name: str) -> None:
    response = _queue_client().pop(name)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    value = response.bytes_value().decode(errors="replace")
    write_stream(value if value.endswith("\n") else f"{value}\n")


@queue_app.command("peek")
def queue_peek(ctx: typer.Context, name: str) -> None:
    response = _queue_client().peek(name)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    value = response.bytes_value().decode(errors="replace")
    write_stream(value if value.endswith("\n") else f"{value}\n")


@queue_app.command("size")
def queue_size(ctx: typer.Context, name: str) -> None:
    response = _queue_client().size(name)
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Queue size",
        fields={"queue": name, "messages": response.size},
    )


@queue_app.command("delete")
def queue_delete(ctx: typer.Context, name: str) -> None:
    _queue_client().delete(name)
    emit_notice(
        ctx,
        payload={"name": name, "deleted": True},
        title="Queue deleted",
        message=f"Deleted {name}.",
    )


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
                [item.name, str(item.count), bytes_count(item.size_bytes), str(item.expiring_keys)]
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
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Map value saved",
        fields={"map": name, "key": key, "expires in": duration(ttl_seconds)},
        tone="success",
    )


@map_app.command("get")
def map_get(ctx: typer.Context, name: str, key: str) -> None:
    response = _map_client().get(name, key)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    value = response.bytes_value().decode(errors="replace")
    write_stream(value if value.endswith("\n") else f"{value}\n")


@map_app.command("keys")
def map_keys(ctx: typer.Context, name: str, cursor: str = "", search: str = "") -> None:
    response = _map_client().keys(name, cursor=cursor, search=search)
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    console.print(
        table(
            "Map keys",
            ["key"],
            [[key] for key in response.data],
            empty=f"No keys in {name}.",
        )
    )
    if response.next:
        console.print(f"More keys available. Continue with --cursor {response.next}")


@map_app.command("delete-key")
def map_delete_key(ctx: typer.Context, name: str, key: str) -> None:
    _map_client().delete(name, key)
    emit_notice(
        ctx,
        payload={"map": name, "key": key, "deleted": True},
        title="Map key deleted",
        message=f"Deleted {key} from {name}.",
    )


@map_app.command("delete")
def map_delete(ctx: typer.Context, name: str) -> None:
    _map_client().delete_map(name)
    emit_notice(
        ctx,
        payload={"name": name, "deleted": True},
        title="Map deleted",
        message=f"Deleted {name}.",
    )


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
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Container started",
        fields={
            "name": response.name,
            "status": response.status.value,
            "image": response.image,
            "id": response.id,
        },
        tone="success",
    )


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
        [
            item.name,
            item.status.value,
            str(item.exit_code) if item.exit_code is not None else "",
            item.id,
        ]
        for item in containers
    ]
    console.print(table("Containers", ["name", "status", "exit", "id"], rows))


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
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
    query: Annotated[str | None, typer.Option("--query")] = None,
) -> None:
    selected = current_workspace(workspace)
    response = admin_api_client(selected).logs(
        LogQueryRequest(
            workspace_id=selected,
            container_id=container_id,
            limit=limit,
            cursor=cursor,
            query=query,
        )
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    if not response.data:
        console.print(empty_state("Container logs", "No log entries found."))
        return
    for entry in response.data:
        print_stream_message(entry.stream, entry.message)


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
    print_events_table("Container events", list(response.data))


def container_delete(
    ctx: typer.Context,
    container_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    admin_api_client(workspace).delete_container(container_id)
    emit_notice(
        ctx,
        payload={"container_id": container_id, "deleted": True},
        title="Container deleted",
        message=f"Deleted {container_id}.",
    )


def container_stop(
    ctx: typer.Context,
    container_ids: Annotated[list[str], typer.Argument()],
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    client = admin_api_client(workspace)
    stopped = [client.stop_container(container_id) for container_id in container_ids]
    emit_result(
        ctx,
        payload=[item.model_dump(mode="json") for item in stopped],
        title="Containers stopped",
        fields={"containers": len(stopped)},
        tone="success",
    )


def unit_create(
    ctx: typer.Context,
    name: str,
    pool: Annotated[str, typer.Option("--pool")] = "",
    provider: Annotated[str, typer.Option("--provider")] = "agent",
    initial_machines: Annotated[int, typer.Option("--initial-machines", min=0)] = 0,
    min_machines: Annotated[int, typer.Option("--min-machines", min=0)] = 0,
    max_machines: Annotated[int, typer.Option("--max-machines", min=0)] = 1,
    scaling_enabled: Annotated[bool, typer.Option("--scaling-enabled")] = False,
    default_eligible: Annotated[bool, typer.Option("--default-eligible")] = False,
    priority: Annotated[
        int,
        typer.Option(
            "--priority",
            help=(
                "Preference over other units serving the same work. Higher is "
                "preferred; work fills the highest tier that fits before the next."
            ),
        ),
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
    response = admin_api_client().create_unit(
        UnitCreateRequest(
            name=name,
            pool=MachinePool(pool),
            provider=provider,
            initial_machines=initial_machines,
            min_machines=min_machines,
            max_machines=max_machines,
            scaling_enabled=scaling_enabled,
            default_eligible=default_eligible,
            priority=priority,
            worker_cpu_millicores=worker_cpu_millicores,
            worker_memory_mib=worker_memory_mib,
            worker_gpu_type=worker_gpu_type,
            worker_gpu_count=worker_gpu_count,
            worker_runtimes=tuple(worker_runtime or (OciRuntimeName.Runsc.value,)),
            worker_preemptible=worker_preemptible,
            idle_drain_timeout_seconds=idle_drain_timeout_seconds,
            registration_timeout_seconds=registration_timeout_seconds,
        )
    )
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Unit created",
        fields={
            "name": response.name,
            "pool": str(response.pool),
            "provider": response.provider,
            "scaling": response.scaling_enabled,
            "id": response.id,
        },
        tone="success",
    )


def unit_ensure(
    ctx: typer.Context,
    name: str,
    pool: Annotated[str, typer.Option("--pool")] = "",
    provider: Annotated[str, typer.Option("--provider")] = "agent",
    capacity_owner_output: Annotated[
        Path | None,
        typer.Option(
            "--capacity-owner-output",
            dir_okay=False,
            resolve_path=True,
            help=(
                "Write the unit's capacity owner id here. A worker is admitted only "
                "into a pool some unit already feeds, and this id is assigned at "
                "creation rather than derived from the pool name."
            ),
        ),
    ] = None,
) -> None:
    """Return the unit with this name, creating it only if none exists.

    Looked up by name rather than by pool: several units may feed one pool, so a
    pool lookup can return somebody else's unit. The shared fleet and a joined
    machine both file a unit against the platform pool, and registering the fleet
    against the machine's unit fails no visible check.
    """
    client = admin_api_client()
    record = next(
        (item for item in client.list_units().pools if item.name == name),
        None,
    )
    if record is None:
        record = client.create_unit(
            UnitCreateRequest(name=name, pool=MachinePool(pool), provider=provider)
        )
    if capacity_owner_output is not None:
        # An identifier, not a credential: it grants nothing without the worker
        # token that accompanies it, so it is written in the clear.
        capacity_owner_output.write_text(record.capacity_owner_id)
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Unit ready",
        fields={
            "name": record.name,
            "pool": str(record.pool),
            "provider": record.provider,
            "id": record.id,
        },
        tone="success",
    )


def unit_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_units().pools
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    console.print(
        table(
            "Units",
            ["name", "pool", "provider", "machines", "scaling", "id"],
            [
                [
                    item.name,
                    item.pool,
                    item.provider,
                    (
                        f"{item.min_machines} min, {item.initial_machines} initial, "
                        f"{item.max_machines} max"
                    ),
                    item.scaling_enabled,
                    item.id,
                ]
                for item in records
            ],
        )
    )


def unit_delete(ctx: typer.Context, unit_id: str) -> None:
    admin_api_client().delete_unit(unit_id)
    emit_notice(
        ctx,
        payload={"unit_id": unit_id, "deleted": True},
        title="Unit deleted",
        message=f"Deleted {unit_id}.",
    )


def unit_clear_degraded(
    ctx: typer.Context,
    unit_id: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = admin_api_client(workspace).clear_unit_degradation(unit_id)
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Pool degradation cleared",
        fields={
            "unit": response.name,
            "phase": response.phase.value,
            "status": response.status,
            "machines": response.observed_machines,
        },
        tone="success",
    )


def pool_join_token(
    ctx: typer.Context,
    pool: Annotated[str, typer.Option("--pool")] = "",
    ttl: Annotated[str, typer.Option("--ttl")] = "",
) -> None:
    """Mint a single-use join credential for a pool, creating its unit if new.

    Names a pool, not a unit: the capacity owner is found or created server-side,
    so nothing has to exist before the first host joins.
    """
    response = admin_api_client().create_pool_join_token(
        MachineJoinCommandRequest(pool=MachinePool(pool), ttl=ttl)
    )
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Pool join token",
        fields={"token": response.token, "expires": timestamp(response.expires_at)},
        tone="success",
        message="Copy this token now. It grants one machine access to the pool.",
    )


def unit_join_token(
    ctx: typer.Context,
    unit_id: str,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
) -> None:
    """Mint a single-use join credential for the unit's pool."""
    response = admin_api_client().create_unit_join_token(unit_id, UnitJoinTokenRequest(ttl=ttl))
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Unit join token",
        fields={"token": response.token, "expires": timestamp(response.expires_at)},
        tone="success",
        message="Copy this token now. It grants one machine access to the pool.",
    )


def pool_join(
    ctx: typer.Context,
    unit_id: str,
    ttl: Annotated[str, typer.Option("--ttl")] = "",
    agent_bin: Annotated[str, typer.Option("--agent-bin")] = "",
    executor: Annotated[str, typer.Option("--executor")] = "",
    worker_image: Annotated[str, typer.Option("--worker-image")] = "",
    print_only: Annotated[bool, typer.Option("--print-only")] = False,
) -> None:
    response = admin_api_client().unit_join_command(unit_id, UnitJoinCommandRequest(ttl=ttl))
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
        console.print(
            notice_card(
                "Unit join command",
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
    emit_notice(
        ctx,
        payload={"status": "running"},
        title="Agent is running",
        message="The unit joined successfully.",
    )


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
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Machine created",
        fields={
            "provider": response.provider,
            "pool": str(response.pool),
            "status": response.status.value,
            "id": response.id,
        },
        tone="success",
    )


def machine_delete(ctx: typer.Context, machine_id: str) -> None:
    admin_api_client().delete_machine(machine_id)
    emit_notice(
        ctx,
        payload={"machine_id": machine_id, "deleted": True},
        title="Machine deleted",
        message=f"Deleted {machine_id}.",
    )


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
    _show_machine_worker_action(ctx, machine_id, action="cordon")


def machine_uncordon(ctx: typer.Context, machine_id: str) -> None:
    _show_machine_worker_action(ctx, machine_id, action="uncordon")


def machine_drain(ctx: typer.Context, machine_id: str) -> None:
    _show_machine_worker_action(ctx, machine_id, action="drain")


def _show_machine_worker_action(
    ctx: typer.Context,
    machine_id: str,
    *,
    action: str,
) -> None:
    payload = _machine_workers(machine_id, action=action)
    emit_result(
        ctx,
        payload=payload,
        title=f"Machine {action} complete",
        fields={"machine": machine_id, "workers": len(payload)},
        tone="success",
    )


@worker_app.command("list")
def worker_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_workers().workers
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
        return
    console.print(
        table(
            "Workers",
            ["pool", "status", "free cpu", "free memory", "free gpu", "machine", "id"],
            [
                [
                    item.pool,
                    item.status,
                    str(item.free_cpu),
                    f"{item.free_memory} MiB",
                    str(item.free_gpu_count),
                    item.machine_id,
                    item.id,
                ]
                for item in records
            ],
        )
    )


@worker_app.command("delete")
def worker_delete(ctx: typer.Context, worker_id: str) -> None:
    admin_api_client().delete_worker(worker_id)
    emit_notice(
        ctx,
        payload={"worker_id": worker_id, "deleted": True},
        title="Worker deleted",
        message=f"Deleted {worker_id}.",
    )


@worker_app.command("cordon")
def worker_cordon(ctx: typer.Context, worker_id: str) -> None:
    record = compute_client().cordon_worker(worker_id)
    _show_worker_action(ctx, record.model_dump(mode="json"), record.id, record.status, "cordoned")


@worker_app.command("uncordon")
def worker_uncordon(ctx: typer.Context, worker_id: str) -> None:
    record = compute_client().uncordon_worker(worker_id)
    _show_worker_action(
        ctx,
        record.model_dump(mode="json"),
        record.id,
        record.status,
        "uncordoned",
    )


@worker_app.command("drain")
def worker_drain(ctx: typer.Context, worker_id: str) -> None:
    response = compute_client().drain_worker(worker_id)
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Worker drained",
        fields={
            "worker": response.worker.id,
            "status": response.worker.status,
            "containers stopped": len(response.stopped_container_ids),
        },
        tone="success",
    )


def _show_worker_action(
    ctx: typer.Context,
    payload: dict[str, JsonValue],
    worker_id: str,
    status: str,
    action: str,
) -> None:
    emit_result(
        ctx,
        payload=payload,
        title=f"Worker {action}",
        fields={"worker": worker_id, "status": status},
        tone="success",
    )


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


unit_app.command("create")(unit_create)
unit_app.command("ensure")(unit_ensure)
unit_app.command("list")(unit_list)
unit_app.command("delete")(unit_delete)
unit_app.command("join")(pool_join)
unit_app.command("join-token")(unit_join_token)
unit_app.command(
    "clear-degraded",
    help="Allow a pool that exhausted its retries to launch machines again.",
)(unit_clear_degraded)


def register_machine_extensions(group: typer.Typer) -> None:
    group.command("join-token")(pool_join_token)
    group.command("create")(machine_create)
    group.command("delete")(machine_delete)
    group.command("cordon")(machine_cordon)
    group.command("uncordon")(machine_uncordon)
    group.command("drain")(machine_drain)


__all__ = [
    "container_app",
    "map_app",
    "queue_app",
    "register_machine_extensions",
    "unit_app",
    "worker_app",
]

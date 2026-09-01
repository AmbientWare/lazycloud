from __future__ import annotations

import shlex
from pathlib import Path
from typing import Annotated

import typer
from lazycloud.cli.components.formatting import timestamp
from lazycloud.cli.components.output import console, json_output_enabled, print_payload, table
from lazycloud.cli.components.results import emit_notice, emit_result
from lazycloud.json_contracts import validate_json_object
from shared.app_identity import AGENT_NAME, STATE_DIR
from shared.compute_policy import MachinePool
from shared.http.operations import AgentLeaseRequest, AgentRegisterRequest

from cli.api_client import admin_api_client
from cli.parameters import parse_key_values

agent_app = typer.Typer(help="Manage agents and leases.")


@agent_app.command("install")
def agent_install(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name")] = "agent",
    pool: Annotated[str, typer.Option("--pool")] = "default",
    endpoint: Annotated[str, typer.Option("--endpoint")] = "http://127.0.0.1:9000",
    version: Annotated[str, typer.Option("--version")] = "local",
    join_token: Annotated[str, typer.Option("--join-token")] = "",
    labels: Annotated[
        list[str] | None,
        typer.Option("--label", help="Agent label as KEY=VALUE."),
    ] = None,
    target: Annotated[str, typer.Option("--target")] = "auto",
    scope: Annotated[str, typer.Option("--scope")] = "auto",
    state_dir: Annotated[str, typer.Option("--state-dir")] = f"{STATE_DIR}/agent",
    binary_path: Annotated[str, typer.Option("--binary")] = AGENT_NAME,
    worker_image: Annotated[str, typer.Option("--worker-image")] = "",
    executor: Annotated[str, typer.Option("--executor")] = "container",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    from cli.agent_install import (
        AgentInstallRequest,
        AgentServiceManager,
        AgentServiceScope,
        install_agent_service,
    )

    try:
        result = install_agent_service(
            AgentInstallRequest(
                name=name,
                pool=MachinePool(pool),
                endpoint=endpoint,
                version=version,
                join_token=join_token,
                labels=parse_key_values(labels or []),
                manager=AgentServiceManager(target),
                scope=AgentServiceScope(scope),
                state_dir=state_dir,
                binary_path=binary_path,
                worker_image=worker_image,
                executor=executor,
            ),
            dry_run=dry_run,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = result.model_dump(mode="json")
    emit_result(
        ctx,
        payload=payload,
        title="Agent service planned" if result.dry_run else "Agent service installed",
        fields={
            "state": result.state.value,
            "service": result.service_name,
            "manager": result.manager.value,
            "scope": result.scope.value,
            "reason": result.reason,
        },
        tone="info" if result.dry_run else "success",
    )


@agent_app.command("join")
def agent_join(
    ctx: typer.Context,
    name: Annotated[str, typer.Option("--name")] = "agent",
    pool: Annotated[str, typer.Option("--pool")] = "default",
    endpoint: Annotated[str, typer.Option("--endpoint")] = "http://127.0.0.1:9000",
    version: Annotated[str, typer.Option("--version")] = "local",
    token_secret: Annotated[str | None, typer.Option("--token-secret")] = None,
    labels: Annotated[
        list[str] | None,
        typer.Option("--label", help="Agent label as KEY=VALUE."),
    ] = None,
) -> None:
    from agent.operations import AgentJoinRequest, build_join_command

    request = AgentJoinRequest(
        name=name,
        pool=MachinePool(pool),
        endpoint=endpoint,
        version=version,
        token_secret=token_secret,
        labels=parse_key_values(labels or []),
    )
    record = admin_api_client().register_agent(
        AgentRegisterRequest(
            name=name,
            pool=MachinePool(pool),
            version=version,
            labels=request.labels,
        )
    )
    command = build_join_command(request)
    payload = validate_json_object(
        {
            "agent": record.model_dump(mode="json"),
            "command": command,
        }
    )
    emit_result(
        ctx,
        payload=payload,
        title="Agent registered",
        fields={
            "name": record.name,
            "pool": str(record.pool),
            "status": record.status.value,
            "id": record.id,
            "command": shlex.join(command),
        },
        tone="success",
        message="Run the command on the agent host.",
    )


@agent_app.command("preflight")
def agent_preflight(
    ctx: typer.Context,
    paths: Annotated[list[Path] | None, typer.Argument()] = None,
) -> None:
    from agent.service_manager import run_preflight_checks

    checks = run_preflight_checks(paths or [])
    payload = [item.model_dump(mode="json") for item in checks]
    if json_output_enabled(ctx):
        print_payload(ctx, payload)
        return
    rows = [[item.name, item.status.value, item.message] for item in checks]
    console.print(table("Agent preflight", ["check", "status", "result"], rows))


@agent_app.command("status")
def agent_status(ctx: typer.Context) -> None:
    from agent.operations import summarize_agent_status

    client = admin_api_client()
    agents = client.list_agents().agents
    leases = client.list_leases(include_inactive=False).leases
    summary = summarize_agent_status([item.pool for item in agents], len(leases))
    emit_result(
        ctx,
        payload=summary.model_dump(mode="json"),
        title="Agent status",
        fields={
            "agents": summary.agents,
            "active leases": summary.active_leases,
            "pools": ", ".join(f"{name} {count}" for name, count in summary.pools.items()),
        },
    )


@agent_app.command("register")
def agent_register(
    ctx: typer.Context,
    name: str,
    pool: Annotated[str, typer.Option("--pool")] = "default",
    version: Annotated[str, typer.Option("--version")] = "local",
) -> None:
    record = admin_api_client().register_agent(
        AgentRegisterRequest(name=name, pool=MachinePool(pool), version=version)
    )
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Agent registered",
        fields={
            "name": record.name,
            "pool": str(record.pool),
            "status": record.status.value,
            "id": record.id,
        },
        tone="success",
    )


@agent_app.command("heartbeat")
def agent_heartbeat(ctx: typer.Context, agent_id: str) -> None:
    record = admin_api_client().heartbeat_agent(agent_id)
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Heartbeat recorded",
        fields={
            "agent": record.name,
            "status": record.status.value,
            "last seen": timestamp(record.last_seen_at) if record.last_seen_at else "unknown",
        },
        tone="success",
    )


@agent_app.command("list")
def agent_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_agents().agents
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
    else:
        rows = [[item.name, str(item.pool), item.status.value, item.id] for item in records]
        console.print(table("Agents", ["name", "pool", "status", "id"], rows))


@agent_app.command("lease")
def agent_lease(
    ctx: typer.Context,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    ttl_seconds: Annotated[int, typer.Option("--ttl")] = 300,
) -> None:
    record = admin_api_client().lease_agent(
        agent_id,
        AgentLeaseRequest(
            resource_type=resource_type,
            resource_id=resource_id,
            ttl_seconds=ttl_seconds,
        ),
    )
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Lease created",
        fields={
            "agent": record.agent_id,
            "resource": f"{record.resource_type}/{record.resource_id}",
            "status": record.status.value,
            "expires": timestamp(record.expires_at),
            "id": record.id,
        },
        tone="success",
    )


@agent_app.command("leases")
def agent_leases(
    ctx: typer.Context,
    include_inactive: Annotated[bool, typer.Option("--all")] = False,
) -> None:
    records = admin_api_client().list_leases(include_inactive=include_inactive).leases
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
    else:
        rows = [
            [
                item.agent_id,
                f"{item.resource_type}/{item.resource_id}",
                item.status.value,
                timestamp(item.expires_at),
                item.id,
            ]
            for item in records
        ]
        console.print(table("Leases", ["agent", "resource", "status", "expires", "id"], rows))


@agent_app.command("release")
def agent_release(ctx: typer.Context, lease_id: str) -> None:
    record = admin_api_client().release_lease(lease_id)
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Lease released",
        fields={
            "agent": record.agent_id,
            "resource": f"{record.resource_type}/{record.resource_id}",
            "status": record.status.value,
        },
        tone="success",
    )


@agent_app.command("delete")
def agent_delete(ctx: typer.Context, agent_id: str) -> None:
    admin_api_client().delete_agent(agent_id)
    emit_notice(
        ctx,
        payload={"agent_id": agent_id, "deleted": True},
        title="Agent deleted",
        message=f"Deleted {agent_id}.",
    )

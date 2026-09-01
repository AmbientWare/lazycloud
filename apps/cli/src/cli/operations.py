from __future__ import annotations

from pathlib import Path
from time import sleep
from typing import Annotated

import typer
from lazycloud.cli.components.context import current_workspace
from lazycloud.cli.components.output import (
    console,
    json_output_enabled,
    print_events_table,
    print_payload,
    table,
)
from lazycloud.json_contracts import JsonValue
from shared.autoscaler_state import AutoscalerTargetKind
from shared.http.operations import ImageBuildRequest
from shared.image_building.authoring import ImageBuildStep, ImageBuildStepKind, ImageSpec
from shared.image_building.context import fingerprint_build_context
from shared.image_building.requirements import load_requirements_file

from cli.api_client import admin_api_client
from cli.components.results import emit_result
from cli.parameters import parse_key_values

image_app = typer.Typer(help="Manage image build records.")
cron_app = typer.Typer(help="Manage cron jobs.")
scheduler_app = typer.Typer(help="Run and inspect scheduler passes.")
autoscaler_app = typer.Typer(help="Inspect and control scheduler autoscalers.")
scheduler_app.add_typer(autoscaler_app, name="autoscaler")


def _validate_uv_project(path: Path) -> None:
    if not (path / "pyproject.toml").is_file():
        raise typer.BadParameter("--uv-project must contain pyproject.toml")
    if not (path / "uv.lock").is_file():
        raise typer.BadParameter("--uv-project must contain uv.lock")


@image_app.command("build")
def image_build(
    ctx: typer.Context,
    base: Annotated[str, typer.Option("--base")] = "python:3.12-slim",
    python_version: Annotated[str, typer.Option("--python-version")] = "3.12",
    packages: Annotated[list[str] | None, typer.Option("--package")] = None,
    requirements_files: Annotated[
        list[Path] | None,
        typer.Option("--requirements", exists=True),
    ] = None,
    uv_project: Annotated[
        Path | None,
        typer.Option("--uv-project", exists=True, file_okay=False),
    ] = None,
    uv_extras: Annotated[list[str] | None, typer.Option("--uv-extra")] = None,
    commands: Annotated[list[str] | None, typer.Option("--command")] = None,
    apt_packages: Annotated[list[str] | None, typer.Option("--apt")] = None,
    micromamba_packages: Annotated[list[str] | None, typer.Option("--micromamba")] = None,
    env_values: Annotated[
        list[str] | None,
        typer.Option("--env", help="Image env var as KEY=VALUE."),
    ] = None,
    dockerfile: Annotated[
        Path | None,
        typer.Option("--dockerfile", exists=True, dir_okay=False),
    ] = None,
    context_dir: Annotated[
        Path | None,
        typer.Option("--context", exists=True, file_okay=False),
    ] = None,
    credential_keys: Annotated[list[str] | None, typer.Option("--credential-key")] = None,
    secrets: Annotated[list[str] | None, typer.Option("--secret")] = None,
    gpu_hint: Annotated[str | None, typer.Option("--gpu")] = None,
    image_id: Annotated[str | None, typer.Option("--image-id")] = None,
    tag: Annotated[str | None, typer.Option("--tag")] = None,
) -> None:
    package_list = list(packages or [])
    for requirements_file in requirements_files or []:
        package_list.extend(load_requirements_file(requirements_file))

    if uv_extras and uv_project is None:
        raise typer.BadParameter("--uv-extra requires --uv-project")

    build_steps: list[ImageBuildStep] = []
    if apt_packages:
        build_steps.append(ImageBuildStep(kind=ImageBuildStepKind.Apt, args=list(apt_packages)))
    if micromamba_packages:
        build_steps.append(
            ImageBuildStep(kind=ImageBuildStepKind.Micromamba, args=list(micromamba_packages))
        )
    if uv_project is not None:
        _validate_uv_project(uv_project)
        build_steps.append(
            ImageBuildStep(kind=ImageBuildStepKind.UvProject, args=[".", *(uv_extras or [])])
        )

    dockerfile_content: str | None = None
    context_digest: str | None = None
    context_path_value: str | None = str(context_dir) if context_dir is not None else None
    if uv_project is not None:
        context_path_value = str(uv_project)
        context_digest = fingerprint_build_context(uv_project)
    if dockerfile is not None:
        context_path = context_dir if context_dir is not None else dockerfile.parent
        if uv_project is not None and context_path.resolve() != uv_project.resolve():
            raise typer.BadParameter("--uv-project must match the Dockerfile build context")
        dockerfile_content = dockerfile.read_text(encoding="utf-8")
        context_path_value = str(context_path)
        context_digest = fingerprint_build_context(context_path)

    image = ImageSpec(
        base=base,
        python_version=python_version,
        packages=package_list,
        commands=list(commands or []),
        build_steps=build_steps,
        env=parse_key_values(env_values or []),
        dockerfile=dockerfile_content,
        context_path=context_path_value,
        context_digest=context_digest,
        credential_keys=list(credential_keys or []),
        secrets=list(secrets or []),
        gpu=gpu_hint,
        image_id=image_id,
    )
    record = admin_api_client().create_image_build(ImageBuildRequest(image=image, tag=tag))
    emit_result(
        ctx,
        payload=record.model_dump(mode="json"),
        title="Image build queued",
        fields={
            "status": record.status.value,
            "phase": record.phase.value,
            "tag": record.tag or "untagged",
            "id": record.id,
        },
        tone="success",
    )


@image_app.command("list")
def image_list(ctx: typer.Context) -> None:
    records = admin_api_client().list_image_builds().builds
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in records])
    else:
        rows = [
            [item.id, item.status.value, item.tag or "", item.fingerprint[:12]] for item in records
        ]
        console.print(table("Image builds", ["id", "status", "tag", "fingerprint"], rows))


@cron_app.command("list")
def cron_list(ctx: typer.Context) -> None:
    cron_jobs = admin_api_client().list_cron_jobs().cron_jobs
    if json_output_enabled(ctx):
        print_payload(ctx, [item.model_dump(mode="json") for item in cron_jobs])
    else:
        rows = [[item.name, item.cron, item.deployment_id, str(item.enabled)] for item in cron_jobs]
        console.print(table("Cron jobs", ["name", "cron", "deployment", "enabled"], rows))


@cron_app.command("delete")
def cron_delete(ctx: typer.Context, name: str) -> None:
    admin_api_client().delete_cron_job(name)
    print_payload(
        ctx,
        {"name": name, "deleted": True},
        title="Cron job deleted",
        tone="success",
    )


@cron_app.command("runs")
def cron_runs(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=1_000)] = 100,
    cursor: Annotated[str | None, typer.Option("--cursor")] = None,
) -> None:
    page = admin_api_client().list_cron_job_runs(limit=limit, cursor=cursor)
    runs = page.data
    if json_output_enabled(ctx):
        print_payload(ctx, page.model_dump(mode="json"))
    else:
        rows = [
            [
                item.cron_job,
                str(item.enqueued),
                item.task_id or "",
                item.message_id or "",
                item.reason or "",
            ]
            for item in runs
        ]
        console.print(
            table(
                "Cron job runs",
                ["cron job", "enqueued", "task", "message", "reason"],
                rows,
            )
        )
        if page.next:
            console.print(f"More results  --cursor {page.next}", highlight=False, markup=False)


@scheduler_app.command("tick")
def scheduler_tick(ctx: typer.Context) -> None:
    response = admin_api_client().tick_scheduler()
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Scheduler tick complete",
        fields={
            "runs": len(response.data),
            "enqueued": sum(item.enqueued for item in response.data),
            "skipped": sum(not item.enqueued for item in response.data),
        },
        tone="success",
    )


@scheduler_app.command("dispatch-containers")
def scheduler_dispatch_containers(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit")] = 100,
) -> None:
    response = admin_api_client().dispatch_scheduler_containers(limit=limit)
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Container dispatch complete",
        fields={
            "containers": len(response.dispatches),
            "dispatched": sum(item.status == "dispatched" for item in response.dispatches),
        },
        tone="success",
    )


@scheduler_app.command("run")
def scheduler_run(
    ctx: typer.Context,
    once: Annotated[bool, typer.Option("--once")] = False,
    interval_seconds: Annotated[float, typer.Option("--interval-seconds")] = 1.0,
    container_limit: Annotated[int, typer.Option("--container-limit")] = 100,
    include_cron_jobs: Annotated[bool, typer.Option("--cron-jobs/--no-cron-jobs")] = True,
    include_containers: Annotated[bool, typer.Option("--containers/--no-containers")] = True,
) -> None:
    if interval_seconds <= 0:
        raise typer.BadParameter("--interval-seconds must be positive")
    client = admin_api_client()

    def run_pass() -> tuple[dict[str, JsonValue], int, int]:
        payload: dict[str, JsonValue] = {}
        cron_run_count = 0
        dispatch_count = 0
        if include_cron_jobs:
            cron_response = client.tick_scheduler()
            payload["cron_jobs"] = cron_response.model_dump(mode="json")
            cron_run_count = len(cron_response.data)
        if include_containers:
            dispatch_response = client.dispatch_scheduler_containers(limit=container_limit)
            payload["containers"] = dispatch_response.model_dump(mode="json")
            dispatch_count = len(dispatch_response.dispatches)
        return payload, cron_run_count, dispatch_count

    if once:
        payload, cron_run_count, dispatch_count = run_pass()
        emit_result(
            ctx,
            payload=payload,
            title="Scheduler pass complete",
            fields={"cron runs": cron_run_count, "container dispatches": dispatch_count},
            tone="success",
        )
        return
    try:
        while True:
            run_pass()
            sleep(interval_seconds)
    except KeyboardInterrupt:
        return


@autoscaler_app.command("status")
def autoscaler_status(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    target_kind: Annotated[AutoscalerTargetKind | None, typer.Option("--kind")] = None,
    target_id: Annotated[str | None, typer.Option("--target-id")] = None,
) -> None:
    response = admin_api_client(workspace).autoscaler_status(
        target_kind=target_kind,
        target_id=target_id,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    rows = [
        [
            item.state.target_kind.value,
            item.stub_name or item.state.target_id,
            str(item.autoscaling_enabled),
            str(item.state.current_count),
            str(item.state.desired_count),
            item.state.decision,
            item.state.reason,
        ]
        for item in response.items
    ]
    console.print(
        table(
            "Autoscalers",
            ["kind", "target", "enabled", "current", "desired", "decision", "reason"],
            rows,
        )
    )


@autoscaler_app.command("history")
def autoscaler_history(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    target_id: Annotated[str | None, typer.Option("--target-id")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 100,
) -> None:
    response = admin_api_client(workspace).autoscaler_history(
        target_id=target_id,
        limit=limit,
    )
    if json_output_enabled(ctx):
        print_payload(ctx, response.model_dump(mode="json"))
        return
    print_events_table("Autoscaler history", response.events)


@autoscaler_app.command("reconcile")
def autoscaler_reconcile(
    ctx: typer.Context,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
    target_kind: Annotated[AutoscalerTargetKind | None, typer.Option("--kind")] = None,
    stub_id: Annotated[str | None, typer.Option("--stub-id")] = None,
) -> None:
    response = admin_api_client(current_workspace(workspace)).reconcile_autoscalers(
        target_kind=target_kind,
        stub_id=stub_id,
    )
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Autoscalers reconciled",
        fields={"targets": len(response.results)},
        tone="success",
    )


@autoscaler_app.command("pause")
def autoscaler_pause(
    ctx: typer.Context,
    stub_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = admin_api_client(current_workspace(workspace)).control_autoscaler(
        stub_id_or_name,
        action="pause",
    )
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Autoscaler paused",
        fields={
            "workload": response.stub.name,
            "kind": response.target_kind.value,
            "enabled": response.autoscaling_enabled,
        },
        tone="success",
    )


@autoscaler_app.command("resume")
def autoscaler_resume(
    ctx: typer.Context,
    stub_id_or_name: str,
    workspace: Annotated[str | None, typer.Option("--workspace")] = None,
) -> None:
    response = admin_api_client(current_workspace(workspace)).control_autoscaler(
        stub_id_or_name,
        action="resume",
    )
    emit_result(
        ctx,
        payload=response.model_dump(mode="json"),
        title="Autoscaler resumed",
        fields={
            "workload": response.stub.name,
            "kind": response.target_kind.value,
            "enabled": response.autoscaling_enabled,
        },
        tone="success",
    )

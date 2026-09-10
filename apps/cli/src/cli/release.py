from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from control.release_settings import ReleaseSettings
from control.releases import DeploymentReleaseService
from coordination.redis_client import RedisClient
from lazycloud.cli.components.cards import notice_card
from lazycloud.cli.components.output import emit
from lazycloud.cli.components.results import emit_result
from provider_clients.release import (
    fetch_release_manifest,
    materialize_agent_artifact,
)
from pydantic import JsonValue
from scheduler.state import RedisSchedulerContainerRepository, RedisSchedulerWorkerRepository

release_app = typer.Typer(help="Work with the release a deployment runs.")


@release_app.command("fetch-agent")
def release_fetch_agent(
    ctx: typer.Context,
    into: Annotated[
        Path,
        typer.Option(
            "--into",
            file_okay=False,
            resolve_path=True,
            help="Directory the deployment serves agent binaries from.",
        ),
    ],
) -> None:
    """Put this deployment's agent binary where the control plane serves it.

    A deployment that runs no release has nothing to fetch and says so rather
    than failing: it serves no agent artifact either, and the settings that
    describe one are absent together.
    """

    settings = ReleaseSettings()
    if not settings.manifest_url:
        payload: dict[str, JsonValue] = {
            "fetched": False,
            "reason": "this deployment names no release",
        }
        emit(
            ctx,
            payload=payload,
            view=notice_card("No release configured", "There is no agent binary to fetch."),
        )
        return

    manifest = fetch_release_manifest(
        settings.manifest_url,
        timeout_seconds=settings.fetch_timeout_seconds,
    )
    path = materialize_agent_artifact(manifest, into=into)
    emit_result(
        ctx,
        payload={
            "fetched": True,
            "release": manifest.release_version,
            "version": manifest.agent_artifact_version,
            "path": str(path),
        },
        title="Agent binary fetched",
        fields={
            "release": manifest.release_version,
            "agent": manifest.agent_artifact_version,
            "path": str(path),
        },
        tone="success",
    )


@release_app.command("status")
def release_status(ctx: typer.Context) -> None:
    release = DeploymentReleaseService().state()
    redis = RedisClient.from_settings()
    try:
        workers = RedisSchedulerWorkerRepository(redis).list_workers()
        containers = RedisSchedulerContainerRepository(redis)
        records: list[JsonValue] = [
            {
                "worker_id": worker.worker_id,
                "machine_id": worker.machine_id,
                "status": worker.status.value,
                "current": release.admits(worker.runtime_image, worker.agent_binary_sha256),
                "containers": [
                    record.container_id for record in containers.list_by_worker(worker.worker_id)
                ],
            }
            for worker in workers
        ]
        emit_result(
            ctx,
            payload={"release": release.model_dump(mode="json"), "workers": records},
            title="Active release",
            fields={"version": release.target.version, "generation": release.generation},
        )
    finally:
        redis.close()


__all__ = ["release_app"]

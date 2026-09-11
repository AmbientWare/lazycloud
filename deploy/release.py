"""Build and activate the local Compose release."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter
from shared.releases import ActiveRelease, ReleaseTarget


class Build(BaseModel):
    target: str = ""


class Deployment(BaseModel):
    replicas: int = 1


class Service(BaseModel):
    image: str = ""
    build: Build | None = None
    profiles: list[str] = Field(default_factory=list)
    restart: str = "no"
    deploy: Deployment = Field(default_factory=Deployment)
    environment: dict[str, str | None] = Field(default_factory=dict)


class Compose(BaseModel):
    name: str
    services: dict[str, Service]


class ContainerHealth(BaseModel):
    Status: str


class ContainerState(BaseModel):
    Status: str
    ExitCode: int
    Health: ContainerHealth | None = None


class ContainerConfig(BaseModel):
    Labels: dict[str, str]


class Container(BaseModel):
    Id: str
    Image: str
    State: ContainerState
    Config: ContainerConfig


def run(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True)


def output(*args: str) -> str:
    return subprocess.check_output(list(args), text=True).strip()


def inspect_containers(ids: list[str]) -> list[Container]:
    if not ids:
        return []
    result = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True)
    if result.returncode:
        missing = {f"error: no such object: {identifier}" for identifier in ids}
        errors = result.stderr.strip().splitlines()
        if not errors or any(error.lower() not in missing for error in errors):
            raise RuntimeError(result.stderr or "Docker container inspection failed")
        # Compose can replace a listed container before inspection. Its absence
        # leaves the service pending until the next snapshot sees the replacement.
        print(result.stderr, file=sys.stderr, end="")
    return TypeAdapter(list[Container]).validate_json(result.stdout)


def main() -> None:
    compose = Compose.model_validate_json(output("docker", "compose", "config", "--format", "json"))
    services = {name: service for name, service in compose.services.items() if not service.profiles}
    workers = {
        name
        for name, service in services.items()
        if service.build is not None
        and service.build.target in {"container-worker", "agent-runtime"}
    }
    worker_images = {
        service.image
        for service in services.values()
        if service.build is not None and service.build.target == "container-worker"
    }
    if len(worker_images) != 1:
        raise RuntimeError("Compose must define one container-worker image")
    if any(
        service.environment.get("LAZYCLOUD_RELEASE_MANIFEST_URL") for service in services.values()
    ):
        raise RuntimeError("Local source publication requires an unpinned Compose environment")

    run("build")
    built_images = {service.image for service in services.values() if service.build is not None}
    images = {
        name: output("docker", "image", "inspect", "--format", "{{.Id}}", service.image)
        for name, service in services.items()
        if service.image in built_images
    }
    worker_image = output("docker", "image", "inspect", "--format", "{{.Id}}", worker_images.pop())
    directory = Path(".lazycloud-release")
    directory.mkdir(parents=True, exist_ok=True)
    active_file = directory / "active.json"
    previous = (
        ActiveRelease.model_validate_json(active_file.read_bytes())
        if active_file.exists()
        else None
    )
    revision = output("git", "rev-parse", "HEAD")
    release = ActiveRelease(
        generation=1 if previous is None else previous.generation + 1,
        manifest_url="",
        target=ReleaseTarget(version="local", source_revision=revision, worker_image=worker_image),
    )
    Path(".env.release").write_text(f"WORKER_RUNTIME_IMAGE={worker_image}\n")
    process = subprocess.Popen(["docker", "compose", "up", "-d"])

    try:
        activated = False
        for cycle in range(120):
            ids = output(
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={compose.name}",
            ).splitlines()
            containers = inspect_containers(ids)
            pending: set[str] = set(services)
            ready_counts: dict[str, int] = {}
            observations: list[dict[str, str | int]] = []
            for container in containers:
                name = container.Config.Labels["com.docker.compose.service"]
                if name not in services:
                    continue
                state = container.State
                ready = (
                    state.Status == "running"
                    and (state.Health is None or state.Health.Status == "healthy")
                    if services[name].restart != "no"
                    else state.Status == "exited" and state.ExitCode == 0
                )
                ready = ready and (name not in images or container.Image == images[name])
                observations.append(
                    {
                        "service": name,
                        "state": state.Status,
                        "health": state.Health.Status if state.Health else "none",
                        "exit": state.ExitCode,
                        "image": container.Image,
                    }
                )
                if ready:
                    ready_counts[name] = ready_counts.get(name, 0) + 1
                elif (
                    process.poll() is not None and state.Status == "exited" and state.ExitCode != 0
                ):
                    raise RuntimeError(
                        f"{name} exited with {state.ExitCode}; inspect docker compose logs {name}"
                    )
            pending -= {
                name
                for name, count in ready_counts.items()
                if count == services[name].deploy.replicas
            }
            print(
                json.dumps(
                    {
                        "cycle": cycle,
                        "active": activated,
                        "pending": sorted(pending),
                        "containers": observations,
                    }
                ),
                flush=True,
            )
            if process.poll() not in (None, 0):
                raise RuntimeError("Compose startup failed; inspect the reported services")
            if not activated and not pending - workers:
                staged = directory / "active.json.next"
                staged.write_text(release.model_dump_json() + "\n")
                os.replace(staged, active_file)
                activated = True
            if activated and not pending and process.poll() == 0:
                return
            time.sleep(3)
        raise RuntimeError("Compose release did not converge; inspect the reported services")
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()

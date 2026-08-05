from __future__ import annotations

from dataclasses import dataclass, field

from shared.compute_policy import MachinePool

WorkflowValue = str | float | int | bool | dict[str, str] | dict[str, int] | list[str] | None


@dataclass(slots=True)
class DeploymentOverrides:
    resource: str | None = None
    cpu: float | None = None
    memory: str | None = None
    gpu: str | None = None
    gpu_count: int | None = None
    image: str | None = None
    dockerfile: str | None = None
    context_dir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    ports: dict[str, int] = field(default_factory=dict)
    keep_warm: int | None = None
    tcp: bool | None = None
    pool: MachinePool | None = None
    preemptible: bool | None = None
    entrypoint: list[str] = field(default_factory=list)
    sync_dir: str | None = None
    container_id: str | None = None

    def has_values(self) -> bool:
        return any(
            (
                self.resource,
                self.cpu is not None,
                self.memory,
                self.gpu,
                self.gpu_count is not None,
                self.image,
                self.dockerfile,
                self.env,
                self.secrets,
                self.ports,
                self.keep_warm is not None,
                self.tcp is not None,
                self.pool,
                self.preemptible is not None,
                self.entrypoint,
                self.sync_dir,
                self.container_id,
            )
        )


def build_deployment_overrides(
    *,
    resource: str | None = None,
    cpu: float | None = None,
    memory: str | None = None,
    gpu: str | None = None,
    gpu_count: int | None = None,
    image: str | None = None,
    dockerfile: str | None = None,
    context_dir: str | None = None,
    env: list[str] | None = None,
    secrets: list[str] | None = None,
    ports: list[str] | None = None,
    keep_warm: int | None = None,
    tcp: bool | None = None,
    pool: MachinePool | None = None,
    preemptible: bool | None = None,
    entrypoint: list[str] | None = None,
    sync_dir: str | None = None,
    container_id: str | None = None,
) -> DeploymentOverrides:
    return DeploymentOverrides(
        resource=resource,
        cpu=cpu,
        memory=memory,
        gpu=gpu,
        gpu_count=gpu_count,
        image=image,
        dockerfile=dockerfile,
        context_dir=context_dir,
        env=_parse_env(env or []),
        secrets=list(secrets or []),
        ports=_parse_ports(ports or []),
        keep_warm=keep_warm,
        tcp=tcp,
        pool=pool,
        preemptible=preemptible,
        entrypoint=list(entrypoint or []),
        sync_dir=sync_dir,
        container_id=container_id,
    )


def workflow_kwargs(
    overrides: DeploymentOverrides, **extra: WorkflowValue
) -> dict[str, WorkflowValue]:
    values: dict[str, WorkflowValue] = {
        "resource": overrides.resource,
        "cpu": overrides.cpu,
        "memory": overrides.memory,
        "gpu": overrides.gpu,
        "gpu_count": overrides.gpu_count,
        "image": overrides.image,
        "dockerfile": overrides.dockerfile,
        "context_dir": overrides.context_dir,
        "env": dict(overrides.env),
        "secrets": list(overrides.secrets),
        "ports": dict(overrides.ports),
        "keep_warm": overrides.keep_warm,
        "tcp": overrides.tcp,
        "pool": overrides.pool,
        "preemptible": overrides.preemptible,
        "entrypoint": list(overrides.entrypoint),
        "sync_dir": overrides.sync_dir,
        "container_id": overrides.container_id,
        **extra,
    }
    return {key: value for key, value in values.items() if value not in (None, [], {})}


def _parse_env(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        key, separator, raw_value = value.partition("=")
        if not separator or not key:
            msg = f"environment variable must use KEY=VALUE format: {value}"
            raise ValueError(msg)
        parsed[key] = raw_value
    return parsed


def _parse_ports(values: list[str]) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for value in values:
        name, separator, raw_port = value.partition("=")
        if separator:
            port_name = name
            port_text = raw_port
        else:
            port_text = value
            port_name = f"port-{port_text}"
        parsed[port_name] = int(port_text)
    return parsed

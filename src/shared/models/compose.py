"""
Pydantic models for Docker Compose v3 files.
"""

from pydantic import BaseModel, field_validator

from shared.models.k8s import RestartPolicy


class ComposePort(BaseModel):
    """Port mapping configuration."""

    published: int | str
    target: int | str
    protocol: str = "tcp"

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        """Validate that protocol is one of the supported values."""
        valid_protocols = ["tcp", "udp", "sctp"]
        v_lower = v.lower()
        if v_lower not in valid_protocols:
            raise ValueError(
                f"Invalid protocol '{v}'. Must be one of: {', '.join(valid_protocols)}"
            )
        return v_lower


class ServiceVolume(BaseModel):
    """Volume mount configuration."""

    type: str = "volume"
    source: str | None = None
    target: str | None = None
    read_only: bool = False


class ServiceNetwork(BaseModel):
    """Network configuration."""

    name: str
    aliases: list[str] | None = None


class HealthCheck(BaseModel):
    """Health check configuration for a service."""

    test: str | list[str] | None = None
    interval: str = "30s"
    timeout: str = "10s"
    retries: int = 3
    start_period: str = "40s"
    disable: bool = True


class ResourceConfig(BaseModel):
    """Resource configuration."""

    cpus: str = "0.5"
    memory: str = "512M"


class ResourcesConfig(BaseModel):
    """Resources configuration."""

    limits: ResourceConfig = ResourceConfig()
    reservations: ResourceConfig = ResourceConfig()


class DeployConfig(BaseModel):
    """Deployment configuration (for Swarm/Kubernetes)."""

    replicas: int = 1
    resources: ResourcesConfig = ResourcesConfig()
    restart_policy: RestartPolicy = RestartPolicy.ALWAYS


class ScalingConfig(BaseModel):
    """Scaling configuration."""

    enabled: bool = False
    min: int = 1
    max: int = 3
    cpu: str = "0.7"
    memory: str = "0.7"
    scale_up_policy: str = "conservative"
    scale_down_policy: str = "conservative"


class ComposeService(BaseModel):
    """Docker Compose service definition."""

    name: str
    image: str
    command: str | list[str] | None = None
    ports: list[ComposePort] | None = None
    volumes: list[ServiceVolume] | None = None
    networks: list[ServiceNetwork] | None = None
    deploy: DeployConfig = DeployConfig()
    healthcheck: HealthCheck = HealthCheck()
    scaling: ScalingConfig = ScalingConfig()


class ComposeNetwork(BaseModel):
    """Docker Compose network definition."""

    name: str


class ComposeVolume(BaseModel):
    """Docker Compose volume definition."""

    name: str


class ComposeFile(BaseModel):
    """Complete Docker Compose file structure."""

    version: str | None = None
    services: list[ComposeService] = []
    networks: list[ComposeNetwork] = []
    volumes: list[ComposeVolume] = []

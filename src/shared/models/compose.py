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
    target: str
    read_only: bool = False


class ServiceNetwork(BaseModel):
    """Network configuration."""

    name: str
    aliases: list[str] | None = None


class HealthCheck(BaseModel):
    """Health check configuration for a service."""

    test: str | list[str]
    interval: str | None = "30s"
    timeout: str | None = "10s"
    retries: int | None = 3
    start_period: str | None = "40s"
    disable: bool = True


class ResourceConfig(BaseModel):
    """Resource configuration."""

    cpus: str = "0.5"
    memory: str = "512M"


class ResourcesConfig(BaseModel):
    """Resources configuration."""

    limits: ResourceConfig | None = None
    reservations: ResourceConfig | None = None


class DeployConfig(BaseModel):
    """Deployment configuration (for Swarm/Kubernetes)."""

    replicas: int | None = None
    resources: ResourcesConfig | None = None
    restart_policy: RestartPolicy | None = RestartPolicy.ALWAYS


class ScalingConfig(BaseModel):
    """Scaling configuration."""

    enabled: bool = False
    min: int | None = None
    max: int | None = None
    cpu: str = "0.7"  # 70%
    memory: str = "0.7"  # 70%
    scale_up_policy: str = "conservative"
    scale_down_policy: str = "conservative"


class ComposeService(BaseModel):
    """Docker Compose service definition."""

    name: str | None = None
    image: str | None = None
    command: str | list[str] | None = None
    ports: list[ComposePort] | None = None
    volumes: list[ServiceVolume] | None = None
    networks: list[ServiceNetwork] | None = None
    deploy: DeployConfig | None = None
    healthcheck: HealthCheck | None = None
    scaling: ScalingConfig | None = None


class ComposeNetwork(BaseModel):
    """Docker Compose network definition."""

    name: str | None = None


class ComposeVolume(BaseModel):
    """Docker Compose volume definition."""

    name: str | None = None


class ComposeFile(BaseModel):
    """Complete Docker Compose file structure."""

    version: str | None = None
    services: list[ComposeService] = []
    networks: list[ComposeNetwork] = []
    volumes: list[ComposeVolume] = []
    user_id: str | None = None  # User context for tracking

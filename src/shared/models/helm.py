from typing import Any

from pydantic import BaseModel, Field

from shared.models.k8s import (
    PodSecurityContext,
    PortConfig,
    ProbeConfig,
    Resources,
    RestartPolicy,
    SecurityContext,
    VolumeMount,
    WorkloadType,
)


class CurrentUsage(BaseModel):
    """Model for current usage."""

    cpu: str | None = None
    memory: str | None = None


class VolumeMountExtended(VolumeMount):
    """Extended volume mount with size for Helm values."""

    size: str = "1Gi"


class MetricsValues(BaseModel):
    """Model for metrics/monitoring configuration."""

    enabled: bool = True
    port: str
    path: str = "/metrics"
    annotations: dict[str, str] = {}


class HPAMetric(BaseModel):
    """Model for HPA metric configuration."""

    type: str
    resource: dict[str, Any]


class HPAScalingPolicy(BaseModel):
    """Model for HPA scaling policy."""

    type: str
    value: int
    periodSeconds: int


class HPABehavior(BaseModel):
    """Model for HPA scaling behavior."""

    stabilizationWindowSeconds: int
    policies: list[HPAScalingPolicy]


class HPAValues(BaseModel):
    """Model for HPA configuration."""

    enabled: bool = True
    minReplicas: int = 1
    maxReplicas: int = 10
    metrics: list[HPAMetric] = []
    behavior: dict[str, HPABehavior] = {}


class HealthCheckValues(BaseModel):
    """Model for health check values."""

    enabled: bool = True
    livenessProbe: ProbeConfig | None = None
    readinessProbe: ProbeConfig | None = None


class PortConfigExtended(PortConfig):
    """Extended port config for Helm values that supports port ranges."""

    port: int | str
    target_port: int | str = Field(None, alias="targetPort")


class IngressTLS(BaseModel):
    """Model for ingress TLS configuration."""

    enabled: bool = True


class IngressValues(BaseModel):
    """Model for ingress configuration."""

    enabled: bool = True
    className: str = "alb"
    hostnamePrefix: str
    tls: IngressTLS | None = None
    annotations: dict[str, str] = {}


class ParsedPort(BaseModel):
    """Model for parsed port information."""

    published: int | str  # Can be int or string for port ranges
    target: int | str
    protocol: str = "tcp"
    ip: str | None = None


class ValidationError(BaseModel):
    """Represents a validation error with context."""

    error_type: str
    message: str
    service: str | None = None
    field: str | None = None
    suggestion: str | None = None

    def __str__(self) -> str:
        parts = []
        if self.service:
            parts.append(f"Service '{self.service}'")
        if self.field:
            parts.append(f"field '{self.field}'")

        prefix = " - ".join(parts) + ": " if parts else ""
        result = f"{prefix}{self.message}"

        if self.suggestion:
            result += f"\n  Suggestion: {self.suggestion}"

        return result


class ImageConfig(BaseModel):
    """Model for parsed image configuration."""

    repository: str
    tag: str
    pullPolicy: str


class GlobalValues(BaseModel):
    """Model for global Helm values."""

    deploymentId: str | None = None
    userId: str
    managedBy: str = "lazycloud"
    createdBy: str = "lazycloud-api"
    runtimeClassName: str = "gvisor"
    labels: dict[str, str | None] = {}
    annotations: dict[str, str | None] = {}


class NetworkValues(BaseModel):
    """Model for network configuration values."""

    name: str
    enabled: bool = True
    external: bool = False
    labels: dict[str, str] = {}


class VolumeValues(BaseModel):
    """Model for volume configuration values."""

    name: str
    enabled: bool = True
    size: str = "1Gi"
    accessModes: list[str] = ["ReadWriteOnce"]
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}


class SecretValues(BaseModel):
    """Model for secret configuration values."""

    name: str
    enabled: bool = True
    type: str = "Opaque"
    data: dict[str, str] = {}
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}


class ServiceSecretMount(BaseModel):
    """Model for service secret mount configuration."""

    name: str
    mountPath: str
    readOnly: bool = True


class ServiceNetwork(BaseModel):
    """Model for service network configuration."""

    name: str
    external: bool = False


class ServiceValues(BaseModel):
    """Model for service configuration values."""

    name: str
    enabled: bool = True
    image: ImageConfig
    resourceName: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    workloadType: WorkloadType | None = None
    serviceName: str | None = None
    command: list[str] | None = None
    environment: dict[str, str] | None = None
    ports: list[PortConfigExtended] | None = None
    volumes: list[VolumeMountExtended] | None = None
    secrets: list[ServiceSecretMount] | None = None
    restartPolicy: str = RestartPolicy.ALWAYS.value
    replicas: int | None = None
    resources: Resources | None = None
    healthcheck: HealthCheckValues | None = None
    ingress: IngressValues | None = None
    hpa: HPAValues | None = None
    metrics: MetricsValues | None = None
    networks: list[ServiceNetwork] | None = None
    securityContext: SecurityContext | None = None
    podSecurityContext: PodSecurityContext | None = None


class NamespaceConfig(BaseModel):
    """Model for namespace configuration."""

    name: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}


class HelmNamespaceValues(BaseModel):
    """Model for namespace chart values."""

    namespace: NamespaceConfig


class HelmValues(BaseModel):
    """Model for the complete Helm values structure."""

    global_values: GlobalValues = Field(
        default_factory=lambda: GlobalValues(userId=""), alias="global"
    )
    services: list[ServiceValues] = []
    networks: list[NetworkValues] = []
    volumes: list[VolumeValues] = []
    secrets: list[SecretValues] = []
    imagePullSecrets: list[dict[str, str]] | None = None

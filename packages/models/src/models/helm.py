from typing import Any, Literal

from pydantic import BaseModel, Field

from models.storage import STORAGE_CLASS_EBS, STORAGE_CLASS_EFS
from models.k8s import (
    PodSecurityContext,
    ProbeConfig,
    Protocol,
    Resources,
    RestartPolicy,
    SecurityContext,
    VolumeMount,
    WorkloadType,
)


class PortConfig(BaseModel):
    """Port configuration for Helm values and service definitions."""

    name: str | None = None
    port: int | str  # Service port
    target_port: int | str | None = Field(
        None, alias="targetPort"
    )  # Container/pod port
    protocol: Protocol = Protocol.TCP
    node_port: int | None = Field(None, alias="nodePort")


class CurrentUsage(BaseModel):
    """Model for current usage."""

    cpu: str | None = None
    memory: str | None = None


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


class IngressTLS(BaseModel):
    """Model for ingress TLS configuration."""

    enabled: bool = True


class IngressValues(BaseModel):
    """Model for ingress configuration."""

    enabled: bool = True
    className: str = "alb"
    hostname: str
    tls: IngressTLS | None = None
    annotations: dict[str, str] = {}


class ParsedPort(BaseModel):
    """Model for parsed port information."""

    published: int | str  # Can be int or string for port ranges
    target: int | str
    protocol: str = "tcp"
    ip: str | None = None


class ImageConfig(BaseModel):
    """Model for parsed image configuration."""

    repository: str
    tag: str
    pullPolicy: str


class GlobalValues(BaseModel):
    """Model for global Helm values."""

    deploymentId: str
    workspaceId: str
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
    storageClass: Literal[STORAGE_CLASS_EBS, STORAGE_CLASS_EFS] = STORAGE_CLASS_EBS
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
    command: list[str] | None = None
    args: list[str] | None = None
    workingDir: str | None = None
    environment: dict[str, str] | None = None
    ports: list[PortConfig] | None = None
    volumes: list[VolumeMount] | None = None
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
    terminationGracePeriodSeconds: int | None = None
    imagePullSecrets: list[dict[str, str]] | None = None


class NamespaceConfig(BaseModel):
    """Model for namespace configuration."""

    name: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}


class HelmNamespaceValues(BaseModel):
    """Model for namespace chart values."""

    namespace: NamespaceConfig
    resourceQuota: dict | None = Field(
        default=None,
        description="Dynamic resource quota objects from subscription features",
    )


class HelmValues(BaseModel):
    """Model for the complete Helm values structure."""

    global_values: GlobalValues = Field(
        default_factory=lambda: GlobalValues(deploymentId="", workspaceId=""),
        alias="global",
    )
    services: list[ServiceValues] = []
    networks: list[NetworkValues] = []
    volumes: list[VolumeValues] = []
    secrets: list[SecretValues] = []
    imagePullSecrets: list[dict[str, str]] | None = None
    compose_yaml: str | None = None

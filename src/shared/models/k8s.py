from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkloadType(StrEnum):
    """Kubernetes workload types."""

    DEPLOYMENT = "Deployment"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    REPLICASET = "ReplicaSet"
    JOB = "Job"
    CRONJOB = "CronJob"


class RestartPolicy(StrEnum):
    """Kubernetes restart policy."""

    ALWAYS = "Always"
    ON_FAILURE = "OnFailure"
    NEVER = "Never"


class Protocol(StrEnum):
    """Network protocol types."""

    TCP = "TCP"
    UDP = "UDP"
    SCTP = "SCTP"


class ResourceRequirements(BaseModel):
    """CPU and memory resource specifications."""

    cpu: str | None = None
    memory: str | None = None


class Resources(BaseModel):
    """Kubernetes resource requirements."""

    limits: ResourceRequirements | None = None
    requests: ResourceRequirements | None = None


class PortConfig(BaseModel):
    """Port configuration."""

    name: str | None = None
    port: int
    target_port: int | None = Field(None, alias="targetPort")
    protocol: Protocol = Protocol.TCP
    node_port: int | None = Field(None, alias="nodePort")


class HttpGetProbe(BaseModel):
    """HTTP GET probe configuration."""

    path: str
    port: int
    scheme: str | None = None
    http_headers: list[dict[str, str]] | None = Field(None, alias="httpHeaders")


class ExecProbe(BaseModel):
    """Exec probe configuration."""

    command: list[str]


class TcpSocketProbe(BaseModel):
    """TCP socket probe configuration."""

    port: int


class ProbeConfig(BaseModel):
    """Kubernetes probe configuration."""

    http_get: HttpGetProbe | None = Field(None, alias="httpGet")
    tcp_socket: TcpSocketProbe | None = Field(None, alias="tcpSocket")
    exec: ExecProbe | None = None
    initial_delay_seconds: int | None = Field(None, alias="initialDelaySeconds")
    timeout_seconds: int | None = Field(None, alias="timeoutSeconds")
    period_seconds: int | None = Field(None, alias="periodSeconds")
    success_threshold: int | None = Field(None, alias="successThreshold")
    failure_threshold: int | None = Field(None, alias="failureThreshold")


class VolumeMount(BaseModel):
    """Volume mount configuration."""

    name: str
    mount_path: str = Field(..., alias="mountPath")
    read_only: bool = Field(False, alias="readOnly")
    sub_path: str | None = Field(None, alias="subPath")


class SecurityCapabilities(BaseModel):
    """Security capabilities."""

    drop: list[str] = []
    add: list[str] = []


class SecurityContext(BaseModel):
    """Container security context."""

    run_as_non_root: bool | None = Field(None, alias="runAsNonRoot")
    run_as_user: int | None = Field(None, alias="runAsUser")
    run_as_group: int | None = Field(None, alias="runAsGroup")
    read_only_root_filesystem: bool | None = Field(None, alias="readOnlyRootFilesystem")
    allow_privilege_escalation: bool | None = Field(
        None, alias="allowPrivilegeEscalation"
    )
    privileged: bool | None = None
    capabilities: SecurityCapabilities | None = None


class PodSecurityContext(BaseModel):
    """Pod-level security context."""

    fs_group: int | None = Field(None, alias="fsGroup")
    run_as_user: int | None = Field(None, alias="runAsUser")
    run_as_group: int | None = Field(None, alias="runAsGroup")
    run_as_non_root: bool | None = Field(None, alias="runAsNonRoot")
    supplemental_groups: list[int] | None = Field(None, alias="supplementalGroups")


class LabelSelector(BaseModel):
    """Kubernetes label selector."""

    match_labels: dict[str, str] | None = Field(None, alias="matchLabels")
    match_expressions: list[dict[str, Any]] | None = Field(
        None, alias="matchExpressions"
    )


class ObjectMeta(BaseModel):
    """Kubernetes object metadata."""

    name: str | None = None
    namespace: str | None = None
    labels: dict[str, str] | None = None
    annotations: dict[str, str] | None = None
    uid: str | None = None
    resource_version: str | None = Field(None, alias="resourceVersion")
    generation: int | None = None
    creation_timestamp: str | None = Field(None, alias="creationTimestamp")


class EnvVar(BaseModel):
    """Environment variable configuration."""

    name: str
    value: str | None = None
    value_from: dict[str, Any] | None = Field(None, alias="valueFrom")


class EnvFromSource(BaseModel):
    """Environment variables from source."""

    config_map_ref: dict[str, Any] | None = Field(None, alias="configMapRef")
    secret_ref: dict[str, Any] | None = Field(None, alias="secretRef")
    prefix: str | None = None

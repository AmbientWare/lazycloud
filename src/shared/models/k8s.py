"""Common Kubernetes models shared across the application."""

from enum import StrEnum
from typing import Any, Dict, List, Optional

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

    cpu: Optional[str] = None
    memory: Optional[str] = None


class Resources(BaseModel):
    """Kubernetes resource requirements."""

    limits: Optional[ResourceRequirements] = None
    requests: Optional[ResourceRequirements] = None


class PortConfig(BaseModel):
    """Port configuration."""

    name: Optional[str] = None
    port: int
    target_port: Optional[int] = Field(None, alias="targetPort")
    protocol: Protocol = Protocol.TCP
    node_port: Optional[int] = Field(None, alias="nodePort")


class HttpGetProbe(BaseModel):
    """HTTP GET probe configuration."""

    path: str
    port: int
    scheme: Optional[str] = None
    http_headers: Optional[List[Dict[str, str]]] = Field(None, alias="httpHeaders")


class ExecProbe(BaseModel):
    """Exec probe configuration."""

    command: List[str]


class TcpSocketProbe(BaseModel):
    """TCP socket probe configuration."""

    port: int


class ProbeConfig(BaseModel):
    """Kubernetes probe configuration."""

    http_get: Optional[HttpGetProbe] = Field(None, alias="httpGet")
    tcp_socket: Optional[TcpSocketProbe] = Field(None, alias="tcpSocket")
    exec: Optional[ExecProbe] = None
    initial_delay_seconds: Optional[int] = Field(None, alias="initialDelaySeconds")
    timeout_seconds: Optional[int] = Field(None, alias="timeoutSeconds")
    period_seconds: Optional[int] = Field(None, alias="periodSeconds")
    success_threshold: Optional[int] = Field(None, alias="successThreshold")
    failure_threshold: Optional[int] = Field(None, alias="failureThreshold")


class VolumeMount(BaseModel):
    """Volume mount configuration."""

    name: str
    mount_path: str = Field(..., alias="mountPath")
    read_only: bool = Field(False, alias="readOnly")
    sub_path: Optional[str] = Field(None, alias="subPath")


class SecurityCapabilities(BaseModel):
    """Security capabilities."""

    drop: List[str] = []
    add: List[str] = []


class SecurityContext(BaseModel):
    """Container security context."""

    run_as_non_root: Optional[bool] = Field(None, alias="runAsNonRoot")
    run_as_user: Optional[int] = Field(None, alias="runAsUser")
    run_as_group: Optional[int] = Field(None, alias="runAsGroup")
    read_only_root_filesystem: Optional[bool] = Field(
        None, alias="readOnlyRootFilesystem"
    )
    allow_privilege_escalation: Optional[bool] = Field(
        None, alias="allowPrivilegeEscalation"
    )
    privileged: Optional[bool] = None
    capabilities: Optional[SecurityCapabilities] = None


class PodSecurityContext(BaseModel):
    """Pod-level security context."""

    fs_group: Optional[int] = Field(None, alias="fsGroup")
    run_as_user: Optional[int] = Field(None, alias="runAsUser")
    run_as_group: Optional[int] = Field(None, alias="runAsGroup")
    run_as_non_root: Optional[bool] = Field(None, alias="runAsNonRoot")
    supplemental_groups: Optional[List[int]] = Field(None, alias="supplementalGroups")


class LabelSelector(BaseModel):
    """Kubernetes label selector."""

    match_labels: Optional[Dict[str, str]] = Field(None, alias="matchLabels")
    match_expressions: Optional[List[Dict[str, Any]]] = Field(
        None, alias="matchExpressions"
    )


class ObjectMeta(BaseModel):
    """Kubernetes object metadata."""

    name: Optional[str] = None  # Optional for pod templates
    namespace: Optional[str] = None
    labels: Optional[Dict[str, str]] = None
    annotations: Optional[Dict[str, str]] = None
    uid: Optional[str] = None
    resource_version: Optional[str] = Field(None, alias="resourceVersion")
    generation: Optional[int] = None
    creation_timestamp: Optional[str] = Field(None, alias="creationTimestamp")


class EnvVar(BaseModel):
    """Environment variable configuration."""

    name: str
    value: Optional[str] = None
    value_from: Optional[Dict[str, Any]] = Field(None, alias="valueFrom")


class EnvFromSource(BaseModel):
    """Environment variables from source."""

    config_map_ref: Optional[Dict[str, Any]] = Field(None, alias="configMapRef")
    secret_ref: Optional[Dict[str, Any]] = Field(None, alias="secretRef")
    prefix: Optional[str] = None

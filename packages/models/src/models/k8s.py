from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class WorkloadType(StrEnum):
    """Kubernetes workload types."""

    DEPLOYMENT = "Deployment"
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


class ContainerPort(BaseModel):
    """Container port configuration as returned by Kubernetes API."""

    name: str | None = None
    containerPort: int | str
    hostPort: int | None = None
    hostIP: str | None = None
    protocol: Protocol = Protocol.TCP


class HttpGetProbe(BaseModel):
    """HTTP GET probe configuration."""

    model_config = ConfigDict(populate_by_name=True)

    path: str
    port: int
    scheme: str | None = None
    http_headers: list[dict[str, str]] | None = Field(default=None, alias="httpHeaders")


class ExecProbe(BaseModel):
    """Exec probe configuration."""

    command: list[str]


class TcpSocketProbe(BaseModel):
    """TCP socket probe configuration."""

    port: int


class ProbeConfig(BaseModel):
    """Kubernetes probe configuration."""

    model_config = ConfigDict(populate_by_name=True)

    http_get: HttpGetProbe | None = Field(default=None, alias="httpGet")
    tcp_socket: TcpSocketProbe | None = Field(default=None, alias="tcpSocket")
    exec: ExecProbe | None = None
    initial_delay_seconds: int | None = Field(default=None, alias="initialDelaySeconds")
    timeout_seconds: int | None = Field(default=None, alias="timeoutSeconds")
    period_seconds: int | None = Field(default=None, alias="periodSeconds")
    success_threshold: int | None = Field(default=None, alias="successThreshold")
    failure_threshold: int | None = Field(default=None, alias="failureThreshold")


class VolumeMount(BaseModel):
    """Volume mount configuration."""

    name: str
    mount_path: str = Field(..., alias="mountPath")
    read_only: bool = Field(False, alias="readOnly")
    sub_path: str | None = Field(None, alias="subPath")
    size: str | None = Field(None)  # For PVC size when used in Helm values


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

    model_config = ConfigDict(populate_by_name=True)

    fs_group: int | None = Field(default=None, alias="fsGroup")
    run_as_user: int | None = Field(default=None, alias="runAsUser")
    run_as_group: int | None = Field(default=None, alias="runAsGroup")
    run_as_non_root: bool | None = Field(default=None, alias="runAsNonRoot")
    supplemental_groups: list[int] | None = Field(
        default=None, alias="supplementalGroups"
    )


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
    deletion_timestamp: str | None = Field(None, alias="deletionTimestamp")


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


# ============================================================================
# Container and Pod Specifications
# ============================================================================


class Container(BaseModel):
    """Container specification."""

    name: str
    image: str
    image_pull_policy: str | None = Field("IfNotPresent", alias="imagePullPolicy")
    resources: Resources | None = None
    env: list[EnvVar] | None = None
    env_from: list[EnvFromSource] | None = Field(None, alias="envFrom")
    ports: list[ContainerPort] | None = None
    volume_mounts: list[VolumeMount] | None = Field(None, alias="volumeMounts")
    liveness_probe: ProbeConfig | None = Field(None, alias="livenessProbe")
    readiness_probe: ProbeConfig | None = Field(None, alias="readinessProbe")
    startup_probe: ProbeConfig | None = Field(None, alias="startupProbe")
    security_context: SecurityContext | None = Field(None, alias="securityContext")


class PodSpec(BaseModel):
    """Pod specification."""

    containers: list[Container]
    service_account: str | None = Field(None, alias="serviceAccount")
    service_account_name: str | None = Field(None, alias="serviceAccountName")
    restart_policy: str | None = Field("Always", alias="restartPolicy")
    dns_policy: str | None = Field("ClusterFirst", alias="dnsPolicy")
    security_context: PodSecurityContext | None = Field(None, alias="securityContext")
    volumes: list[dict[str, Any]] | None = None
    runtime_class_name: str | None = Field(None, alias="runtimeClassName")
    scheduler_name: str | None = Field(None, alias="schedulerName")
    termination_grace_period_seconds: int | None = Field(
        None, alias="terminationGracePeriodSeconds"
    )
    automount_service_account_token: bool | None = Field(
        None, alias="automountServiceAccountToken"
    )


class PodTemplateSpec(BaseModel):
    """Pod template specification."""

    metadata: ObjectMeta | None = None
    spec: PodSpec


# ============================================================================
# Workload Specifications
# ============================================================================


class DeploymentSpec(BaseModel):
    """Deployment specification."""

    replicas: int | None = 1
    selector: dict[str, Any]
    template: PodTemplateSpec
    strategy: dict[str, Any] | None = None
    revision_history_limit: int | None = Field(None, alias="revisionHistoryLimit")
    progress_deadline_seconds: int | None = Field(None, alias="progressDeadlineSeconds")


# ============================================================================
# Status Objects
# ============================================================================


class DeploymentStatus(BaseModel):
    """Deployment status."""

    observed_generation: int | None = Field(None, alias="observedGeneration")
    replicas: int | None = 0
    updated_replicas: int | None = Field(0, alias="updatedReplicas")
    ready_replicas: int | None = Field(0, alias="readyReplicas")
    available_replicas: int | None = Field(0, alias="availableReplicas")
    unavailable_replicas: int | None = Field(0, alias="unavailableReplicas")
    conditions: list[dict[str, Any]] | None = None
    collision_count: int | None = Field(None, alias="collisionCount")


class PodStatus(BaseModel):
    """Pod status information."""

    phase: str | None = None  # Pending, Running, Succeeded, Failed, Unknown
    conditions: list[dict[str, Any]] | None = None
    host_ip: str | None = Field(None, alias="hostIP")
    pod_ip: str | None = Field(None, alias="podIP")
    pod_ips: list[dict[str, str]] | None = Field(None, alias="podIPs")
    start_time: str | None = Field(None, alias="startTime")
    container_statuses: list[dict[str, Any]] | None = Field(
        None, alias="containerStatuses"
    )
    init_container_statuses: list[dict[str, Any]] | None = Field(
        None, alias="initContainerStatuses"
    )
    nominated_node_name: str | None = Field(None, alias="nominatedNodeName")
    reason: str | None = None
    message: str | None = None


class HPAStatus(BaseModel):
    """Horizontal Pod Autoscaler status."""

    observed_generation: int | None = Field(None, alias="observedGeneration")
    last_scale_time: str | None = Field(None, alias="lastScaleTime")
    current_replicas: int = Field(0, alias="currentReplicas")
    desired_replicas: int = Field(0, alias="desiredReplicas")
    current_metrics: list[dict[str, Any]] | None = Field(None, alias="currentMetrics")
    conditions: list[dict[str, Any]] | None = None


# ============================================================================
# Complete Resource Objects
# ============================================================================


class Deployment(BaseModel):
    """Kubernetes Deployment resource."""

    api_version: str = Field("apps/v1", alias="apiVersion")
    kind: str = "Deployment"
    metadata: ObjectMeta
    spec: DeploymentSpec
    status: DeploymentStatus | None = None


class Pod(BaseModel):
    """Kubernetes Pod resource."""

    api_version: str = Field("v1", alias="apiVersion")
    kind: str = "Pod"
    metadata: ObjectMeta
    spec: PodSpec
    status: PodStatus | None = None


class PodList(BaseModel):
    """List of Kubernetes Pods."""

    api_version: str = Field("v1", alias="apiVersion")
    kind: str = "PodList"
    items: list[Pod]
    metadata: dict[str, Any] | None = None


class HorizontalPodAutoscaler(BaseModel):
    """Kubernetes Horizontal Pod Autoscaler resource."""

    api_version: str = Field("autoscaling/v2", alias="apiVersion")
    kind: str = "HorizontalPodAutoscaler"
    metadata: ObjectMeta
    spec: dict[str, Any] | None = None
    status: HPAStatus | None = None


class PVCInfo(BaseModel):
    """PVC details including size and volume handle for billing."""

    name: str
    storage_class: str
    requested_size_gb: float = 0.0
    volume_handle: str | None = None

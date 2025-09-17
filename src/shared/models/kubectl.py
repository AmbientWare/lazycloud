from typing import Any

from pydantic import BaseModel, Field

from shared.models.k8s import (
    EnvFromSource,
    EnvVar,
    ObjectMeta,
    PodSecurityContext,
    PortConfig,
    ProbeConfig,
    Resources,
    SecurityContext,
    VolumeMount,
)


class KubectlContainer(BaseModel):
    """Container specification."""

    name: str
    image: str
    image_pull_policy: str | None = Field("IfNotPresent", alias="imagePullPolicy")
    resources: Resources | None = None
    env: list[EnvVar] | None = None
    env_from: list[EnvFromSource] | None = Field(None, alias="envFrom")
    ports: list[PortConfig] | None = None
    volume_mounts: list[VolumeMount] | None = Field(None, alias="volumeMounts")
    liveness_probe: ProbeConfig | None = Field(None, alias="livenessProbe")
    readiness_probe: ProbeConfig | None = Field(None, alias="readinessProbe")
    startup_probe: ProbeConfig | None = Field(None, alias="startupProbe")
    security_context: SecurityContext | None = Field(None, alias="securityContext")


class KubectlPodSpec(BaseModel):
    """Pod specification."""

    containers: list[KubectlContainer]
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


class KubectlPodTemplateSpec(BaseModel):
    """Pod template specification."""

    metadata: ObjectMeta | None = None
    spec: KubectlPodSpec


class KubectlDeploymentSpec(BaseModel):
    """Deployment specification."""

    replicas: int | None = 1
    selector: dict[str, Any]
    template: KubectlPodTemplateSpec
    strategy: dict[str, Any] | None = None
    revision_history_limit: int | None = Field(None, alias="revisionHistoryLimit")
    progress_deadline_seconds: int | None = Field(None, alias="progressDeadlineSeconds")


class KubectlStatefulSetSpec(BaseModel):
    """StatefulSet specification."""

    replicas: int | None = 1
    selector: dict[str, Any]
    template: KubectlPodTemplateSpec
    service_name: str = Field(..., alias="serviceName")
    pod_management_policy: str | None = Field(
        "OrderedReady", alias="podManagementPolicy"
    )
    update_strategy: dict[str, Any] | None = Field(None, alias="updateStrategy")
    volume_claim_templates: list[dict[str, Any]] | None = Field(
        None, alias="volumeClaimTemplates"
    )
    persistent_volume_claim_retention_policy: dict[str, Any] | None = Field(
        None, alias="persistentVolumeClaimRetentionPolicy"
    )
    revision_history_limit: int | None = Field(None, alias="revisionHistoryLimit")


class KubectlDeploymentStatus(BaseModel):
    """Deployment status."""

    observed_generation: int | None = Field(None, alias="observedGeneration")
    replicas: int | None = 0
    updated_replicas: int | None = Field(0, alias="updatedReplicas")
    ready_replicas: int | None = Field(0, alias="readyReplicas")
    available_replicas: int | None = Field(0, alias="availableReplicas")
    unavailable_replicas: int | None = Field(0, alias="unavailableReplicas")
    conditions: list[dict[str, Any]] | None = None
    collision_count: int | None = Field(None, alias="collisionCount")


class KubectlStatefulSetStatus(BaseModel):
    """StatefulSet status."""

    observed_generation: int | None = Field(None, alias="observedGeneration")
    replicas: int | None = 0
    ready_replicas: int | None = Field(0, alias="readyReplicas")
    current_replicas: int | None = Field(0, alias="currentReplicas")
    updated_replicas: int | None = Field(0, alias="updatedReplicas")
    available_replicas: int | None = Field(0, alias="availableReplicas")
    collision_count: int | None = Field(None, alias="collisionCount")
    current_revision: str | None = Field(None, alias="currentRevision")
    update_revision: str | None = Field(None, alias="updateRevision")
    conditions: list[dict[str, Any]] | None = None


class KubectlDeployment(BaseModel):
    """Kubernetes Deployment resource."""

    api_version: str = Field("apps/v1", alias="apiVersion")
    kind: str = "Deployment"
    metadata: ObjectMeta
    spec: KubectlDeploymentSpec
    status: KubectlDeploymentStatus | None = None


class KubectlStatefulSet(BaseModel):
    """Kubernetes StatefulSet resource."""

    api_version: str = Field("apps/v1", alias="apiVersion")
    kind: str = "StatefulSet"
    metadata: ObjectMeta
    spec: KubectlStatefulSetSpec
    status: KubectlStatefulSetStatus | None = None


class KubectlPodStatus(BaseModel):
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


class KubectlPod(BaseModel):
    """Kubernetes Pod resource."""

    api_version: str = Field("v1", alias="apiVersion")
    kind: str = "Pod"
    metadata: ObjectMeta
    spec: KubectlPodSpec
    status: KubectlPodStatus | None = None


class KubectlPodList(BaseModel):
    """List of Kubernetes Pods."""

    api_version: str = Field("v1", alias="apiVersion")
    kind: str = "PodList"
    items: list[KubectlPod]
    metadata: dict[str, Any] | None = None


class KubectlHPAStatus(BaseModel):
    """Horizontal Pod Autoscaler status."""

    observed_generation: int | None = Field(None, alias="observedGeneration")
    last_scale_time: str | None = Field(None, alias="lastScaleTime")
    current_replicas: int = Field(0, alias="currentReplicas")
    desired_replicas: int = Field(0, alias="desiredReplicas")
    current_metrics: list[dict[str, Any]] | None = Field(None, alias="currentMetrics")
    conditions: list[dict[str, Any]] | None = None


class KubectlHPA(BaseModel):
    """Kubernetes Horizontal Pod Autoscaler resource."""

    api_version: str = Field("autoscaling/v2", alias="apiVersion")
    kind: str = "HorizontalPodAutoscaler"
    metadata: ObjectMeta
    spec: dict[str, Any] | None = None
    status: KubectlHPAStatus | None = None

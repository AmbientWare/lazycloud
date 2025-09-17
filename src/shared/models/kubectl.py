"""Pydantic models for Kubernetes API responses."""

from typing import Any, Dict, List, Optional

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
    image_pull_policy: Optional[str] = Field("IfNotPresent", alias="imagePullPolicy")
    resources: Optional[Resources] = None
    env: Optional[List[EnvVar]] = None
    env_from: Optional[List[EnvFromSource]] = Field(None, alias="envFrom")
    ports: Optional[List[PortConfig]] = None
    volume_mounts: Optional[List[VolumeMount]] = Field(None, alias="volumeMounts")
    liveness_probe: Optional[ProbeConfig] = Field(None, alias="livenessProbe")
    readiness_probe: Optional[ProbeConfig] = Field(None, alias="readinessProbe")
    startup_probe: Optional[ProbeConfig] = Field(None, alias="startupProbe")
    security_context: Optional[SecurityContext] = Field(None, alias="securityContext")


class KubectlPodSpec(BaseModel):
    """Pod specification."""

    containers: List[KubectlContainer]
    service_account: Optional[str] = Field(None, alias="serviceAccount")
    service_account_name: Optional[str] = Field(None, alias="serviceAccountName")
    restart_policy: Optional[str] = Field("Always", alias="restartPolicy")
    dns_policy: Optional[str] = Field("ClusterFirst", alias="dnsPolicy")
    security_context: Optional[PodSecurityContext] = Field(
        None, alias="securityContext"
    )
    volumes: Optional[List[Dict[str, Any]]] = None
    runtime_class_name: Optional[str] = Field(None, alias="runtimeClassName")
    scheduler_name: Optional[str] = Field(None, alias="schedulerName")
    termination_grace_period_seconds: Optional[int] = Field(
        None, alias="terminationGracePeriodSeconds"
    )
    automount_service_account_token: Optional[bool] = Field(
        None, alias="automountServiceAccountToken"
    )


class KubectlPodTemplateSpec(BaseModel):
    """Pod template specification."""

    metadata: Optional[ObjectMeta] = None
    spec: KubectlPodSpec


class KubectlDeploymentSpec(BaseModel):
    """Deployment specification."""

    replicas: Optional[int] = 1
    selector: Dict[str, Any]
    template: KubectlPodTemplateSpec
    strategy: Optional[Dict[str, Any]] = None
    revision_history_limit: Optional[int] = Field(None, alias="revisionHistoryLimit")
    progress_deadline_seconds: Optional[int] = Field(
        None, alias="progressDeadlineSeconds"
    )


class KubectlStatefulSetSpec(BaseModel):
    """StatefulSet specification."""

    replicas: Optional[int] = 1
    selector: Dict[str, Any]
    template: KubectlPodTemplateSpec
    service_name: str = Field(..., alias="serviceName")
    pod_management_policy: Optional[str] = Field(
        "OrderedReady", alias="podManagementPolicy"
    )
    update_strategy: Optional[Dict[str, Any]] = Field(None, alias="updateStrategy")
    volume_claim_templates: Optional[List[Dict[str, Any]]] = Field(
        None, alias="volumeClaimTemplates"
    )
    persistent_volume_claim_retention_policy: Optional[Dict[str, Any]] = Field(
        None, alias="persistentVolumeClaimRetentionPolicy"
    )
    revision_history_limit: Optional[int] = Field(None, alias="revisionHistoryLimit")


class KubectlDeploymentStatus(BaseModel):
    """Deployment status."""

    observed_generation: Optional[int] = Field(None, alias="observedGeneration")
    replicas: Optional[int] = 0
    updated_replicas: Optional[int] = Field(0, alias="updatedReplicas")
    ready_replicas: Optional[int] = Field(0, alias="readyReplicas")
    available_replicas: Optional[int] = Field(0, alias="availableReplicas")
    unavailable_replicas: Optional[int] = Field(0, alias="unavailableReplicas")
    conditions: Optional[List[Dict[str, Any]]] = None
    collision_count: Optional[int] = Field(None, alias="collisionCount")


class KubectlStatefulSetStatus(BaseModel):
    """StatefulSet status."""

    observed_generation: Optional[int] = Field(None, alias="observedGeneration")
    replicas: Optional[int] = 0
    ready_replicas: Optional[int] = Field(0, alias="readyReplicas")
    current_replicas: Optional[int] = Field(0, alias="currentReplicas")
    updated_replicas: Optional[int] = Field(0, alias="updatedReplicas")
    available_replicas: Optional[int] = Field(0, alias="availableReplicas")
    collision_count: Optional[int] = Field(None, alias="collisionCount")
    current_revision: Optional[str] = Field(None, alias="currentRevision")
    update_revision: Optional[str] = Field(None, alias="updateRevision")
    conditions: Optional[List[Dict[str, Any]]] = None


class KubectlDeployment(BaseModel):
    """Kubernetes Deployment resource."""

    api_version: str = Field("apps/v1", alias="apiVersion")
    kind: str = "Deployment"
    metadata: ObjectMeta
    spec: KubectlDeploymentSpec
    status: Optional[KubectlDeploymentStatus] = None


class KubectlStatefulSet(BaseModel):
    """Kubernetes StatefulSet resource."""

    api_version: str = Field("apps/v1", alias="apiVersion")
    kind: str = "StatefulSet"
    metadata: ObjectMeta
    spec: KubectlStatefulSetSpec
    status: Optional[KubectlStatefulSetStatus] = None


class KubectlPodStatus(BaseModel):
    """Pod status information."""

    phase: Optional[str] = None  # Pending, Running, Succeeded, Failed, Unknown
    conditions: Optional[List[Dict[str, Any]]] = None
    host_ip: Optional[str] = Field(None, alias="hostIP")
    pod_ip: Optional[str] = Field(None, alias="podIP")
    pod_ips: Optional[List[Dict[str, str]]] = Field(None, alias="podIPs")
    start_time: Optional[str] = Field(None, alias="startTime")
    container_statuses: Optional[List[Dict[str, Any]]] = Field(
        None, alias="containerStatuses"
    )
    init_container_statuses: Optional[List[Dict[str, Any]]] = Field(
        None, alias="initContainerStatuses"
    )
    nominated_node_name: Optional[str] = Field(None, alias="nominatedNodeName")
    reason: Optional[str] = None
    message: Optional[str] = None


class KubectlPod(BaseModel):
    """Kubernetes Pod resource."""

    api_version: str = Field("v1", alias="apiVersion")
    kind: str = "Pod"
    metadata: ObjectMeta
    spec: KubectlPodSpec
    status: Optional[KubectlPodStatus] = None


class KubectlPodList(BaseModel):
    """List of Kubernetes Pods."""

    api_version: str = Field("v1", alias="apiVersion")
    kind: str = "PodList"
    items: List[KubectlPod]
    metadata: Optional[Dict[str, Any]] = None


class KubectlHPAStatus(BaseModel):
    """Horizontal Pod Autoscaler status."""

    observed_generation: Optional[int] = Field(None, alias="observedGeneration")
    last_scale_time: Optional[str] = Field(None, alias="lastScaleTime")
    current_replicas: int = Field(0, alias="currentReplicas")
    desired_replicas: int = Field(0, alias="desiredReplicas")
    current_metrics: Optional[List[Dict[str, Any]]] = Field(
        None, alias="currentMetrics"
    )
    conditions: Optional[List[Dict[str, Any]]] = None


class KubectlHPA(BaseModel):
    """Kubernetes Horizontal Pod Autoscaler resource."""

    api_version: str = Field("autoscaling/v2", alias="apiVersion")
    kind: str = "HorizontalPodAutoscaler"
    metadata: ObjectMeta
    spec: Optional[Dict[str, Any]] = None
    status: Optional[KubectlHPAStatus] = None

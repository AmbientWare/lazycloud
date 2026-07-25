from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeGuard

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from shared.app_identity import NAME
from shared.capacity import CAPACITY_OWNER_ID_PATTERN

_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
_POOL_LABEL = re.compile(r"[A-Za-z0-9](?:[-A-Za-z0-9_.]*[A-Za-z0-9])?")
_HELM_MANAGER = "Helm"
_SCALING_OWNER = "scheduler-worker-pool-controller"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_COMPONENT_LABEL = "app.kubernetes.io/component"
_POOL_LABEL_KEY = f"{NAME}.io/worker-pool"
_SCALING_OWNER_ANNOTATION = f"{NAME}.io/scaling-owner"
_CAPACITY_OWNER_ANNOTATION = f"{NAME}.io/capacity-owner-id"


class _KubernetesConfigLoader(Protocol):
    def load_incluster_config(self) -> None: ...

    def load_kube_config(self) -> None: ...


class _KubernetesAppsApi(Protocol):
    def read_namespaced_deployment(
        self,
        *,
        name: str,
        namespace: str,
    ) -> client.V1Deployment: ...

    def patch_namespaced_deployment_scale(
        self,
        *,
        name: str,
        namespace: str,
        body: dict[str, JsonValue],
    ) -> client.V1Scale: ...


class _KubernetesSerializer(Protocol):
    def sanitize_for_serialization(
        self,
        response: client.V1Deployment | client.V1Scale,
    ) -> JsonValue: ...


def _is_config_loader(value: object) -> TypeGuard[_KubernetesConfigLoader]:
    return all(
        callable(getattr(value, operation, None))
        for operation in ("load_incluster_config", "load_kube_config")
    )


def _is_apps_api(value: object) -> TypeGuard[_KubernetesAppsApi]:
    return all(
        callable(getattr(value, operation, None))
        for operation in (
            "read_namespaced_deployment",
            "patch_namespaced_deployment_scale",
        )
    )


def _is_serializer(value: object) -> TypeGuard[_KubernetesSerializer]:
    return callable(getattr(value, "sanitize_for_serialization", None))


class _KubernetesApiStatus(Protocol):
    status: int | None


def _has_kubernetes_api_status(value: object) -> TypeGuard[_KubernetesApiStatus]:
    status = getattr(value, "status", None)
    return status is None or isinstance(status, int)


class KubernetesModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KubernetesContainerWorkerScalerSettings(KubernetesModel):
    deployment_prefix: str = NAME
    namespace: str = f"{NAME}-system"

    @field_validator("deployment_prefix", "namespace")
    @classmethod
    def validate_kubernetes_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) > 63 or _DNS_LABEL.fullmatch(normalized) is None:
            raise ValueError("Kubernetes names must be DNS labels of at most 63 characters")
        return normalized


class KubernetesContainerWorkerScaleTarget(KubernetesModel):
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    pool_name: str
    namespace: str
    deployment_name: str
    desired_replicas: int = Field(ge=0)
    minimum_replicas: int = Field(ge=0)
    maximum_replicas: int = Field(ge=0)
    reservation_id: str = Field(min_length=1, max_length=253)
    operation_id: str = Field(min_length=1, max_length=253)

    @field_validator("capacity_owner_id", "reservation_id", "operation_id", mode="before")
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        return value.strip()

    @field_validator("namespace", "deployment_name")
    @classmethod
    def validate_resource_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) > 63 or _DNS_LABEL.fullmatch(normalized) is None:
            raise ValueError("Kubernetes resource names must be DNS labels")
        return normalized

    @field_validator("pool_name")
    @classmethod
    def validate_pool_name(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 63 or _POOL_LABEL.fullmatch(normalized) is None:
            raise ValueError("worker pool name must be a Kubernetes label value")
        return normalized

    @model_validator(mode="after")
    def validate_replica_bounds(self) -> KubernetesContainerWorkerScaleTarget:
        if self.maximum_replicas < self.minimum_replicas:
            raise ValueError("maximum replicas must be greater than or equal to minimum replicas")
        return self


class KubernetesContainerWorkerDeploymentState(KubernetesModel):
    capacity_owner_id: str
    pool_name: str
    namespace: str
    deployment_name: str
    desired_replicas: int = Field(ge=0)
    observed_replicas: int = Field(ge=0)
    resource_version: str = Field(min_length=1)


class KubernetesContainerWorkerScaleOutcome(StrEnum):
    ExistingPending = "existing_pending"
    Requested = "requested"
    AtLimit = "at_limit"
    TemporarilyUnavailable = "temporarily_unavailable"
    Unsupported = "unsupported"


class KubernetesContainerWorkerScaleResult(KubernetesModel):
    outcome: KubernetesContainerWorkerScaleOutcome
    target: KubernetesContainerWorkerScaleTarget
    state: KubernetesContainerWorkerDeploymentState | None = None
    retry_after_seconds: float | None = Field(default=None, ge=0)
    reason: str = Field(min_length=1)


class KubernetesContainerWorkerScaleApi(Protocol):
    """Authenticated Kubernetes Deployment and scale-subresource boundary."""

    def describe_helm_container_worker(
        self,
        target: KubernetesContainerWorkerScaleTarget,
    ) -> KubernetesContainerWorkerDeploymentState: ...

    def compare_and_set_helm_container_worker(
        self,
        target: KubernetesContainerWorkerScaleTarget,
        *,
        resource_version: str,
    ) -> KubernetesContainerWorkerDeploymentState: ...


class _KubernetesResponseModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _DeploymentMetadata(_KubernetesResponseModel):
    name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    resource_version: str = Field(alias="resourceVersion", min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class _DeploymentSpec(_KubernetesResponseModel):
    replicas: int = Field(ge=0)


class _DeploymentStatus(_KubernetesResponseModel):
    replicas: int = Field(default=0, ge=0)


class _DeploymentResource(_KubernetesResponseModel):
    metadata: _DeploymentMetadata
    spec: _DeploymentSpec
    status: _DeploymentStatus = Field(default_factory=_DeploymentStatus)


class _ScaleMetadata(_KubernetesResponseModel):
    resource_version: str = Field(alias="resourceVersion", min_length=1)


class _ScaleResource(_KubernetesResponseModel):
    metadata: _ScaleMetadata
    spec: _DeploymentSpec
    status: _DeploymentStatus = Field(default_factory=_DeploymentStatus)


_DEPLOYMENT_ADAPTER = TypeAdapter(_DeploymentResource)
_SCALE_ADAPTER = TypeAdapter(_ScaleResource)


class KubernetesDeploymentOwnershipError(RuntimeError):
    pass


@dataclass(slots=True)
class KubernetesAppsScaleApi:
    apps_api: _KubernetesAppsApi
    serializer: _KubernetesSerializer

    @classmethod
    def from_cluster(cls) -> KubernetesAppsScaleApi:
        config_loader: object = config
        if not _is_config_loader(config_loader):
            raise RuntimeError("Kubernetes configuration loader is incomplete")
        try:
            config_loader.load_incluster_config()
        except config.ConfigException:
            config_loader.load_kube_config()
        apps_api: object = client.AppsV1Api()
        serializer: object = client.ApiClient()
        if not _is_apps_api(apps_api):
            raise RuntimeError("Kubernetes Apps API is incomplete")
        if not _is_serializer(serializer):
            raise RuntimeError("Kubernetes API serializer is incomplete")
        return cls(apps_api, serializer)

    def describe_helm_container_worker(
        self,
        target: KubernetesContainerWorkerScaleTarget,
    ) -> KubernetesContainerWorkerDeploymentState:
        response = self.apps_api.read_namespaced_deployment(
            name=target.deployment_name,
            namespace=target.namespace,
        )
        if not isinstance(response, client.V1Deployment):
            raise RuntimeError("Kubernetes API returned an invalid Deployment response")
        deployment = self._deployment(response)
        self._verify_helm_ownership(deployment.metadata, target)
        return self._deployment_state(
            target,
            desired_replicas=deployment.spec.replicas,
            observed_replicas=deployment.status.replicas,
            resource_version=deployment.metadata.resource_version,
        )

    def compare_and_set_helm_container_worker(
        self,
        target: KubernetesContainerWorkerScaleTarget,
        *,
        resource_version: str,
    ) -> KubernetesContainerWorkerDeploymentState:
        response = self.apps_api.patch_namespaced_deployment_scale(
            name=target.deployment_name,
            namespace=target.namespace,
            body={
                "metadata": {"resourceVersion": resource_version},
                "spec": {"replicas": target.desired_replicas},
            },
        )
        if not isinstance(response, client.V1Scale):
            raise RuntimeError("Kubernetes API returned an invalid scale response")
        scale = self._scale(response)
        return self._deployment_state(
            target,
            desired_replicas=scale.spec.replicas,
            observed_replicas=scale.status.replicas,
            resource_version=scale.metadata.resource_version,
        )

    def _deployment(self, response: client.V1Deployment) -> _DeploymentResource:
        serialized = self.serializer.sanitize_for_serialization(response)
        try:
            return _DEPLOYMENT_ADAPTER.validate_python(serialized)
        except ValidationError as exc:
            raise RuntimeError("Kubernetes Deployment response is incomplete") from exc

    def _scale(self, response: client.V1Scale) -> _ScaleResource:
        serialized = self.serializer.sanitize_for_serialization(response)
        try:
            return _SCALE_ADAPTER.validate_python(serialized)
        except ValidationError as exc:
            raise RuntimeError("Kubernetes scale response is incomplete") from exc

    @staticmethod
    def _deployment_state(
        target: KubernetesContainerWorkerScaleTarget,
        *,
        desired_replicas: int,
        observed_replicas: int,
        resource_version: str,
    ) -> KubernetesContainerWorkerDeploymentState:
        return KubernetesContainerWorkerDeploymentState(
            capacity_owner_id=target.capacity_owner_id,
            pool_name=target.pool_name,
            namespace=target.namespace,
            deployment_name=target.deployment_name,
            desired_replicas=desired_replicas,
            observed_replicas=observed_replicas,
            resource_version=resource_version,
        )

    @staticmethod
    def _verify_helm_ownership(
        metadata: _DeploymentMetadata,
        target: KubernetesContainerWorkerScaleTarget,
    ) -> None:
        expected_labels = {
            _MANAGED_BY_LABEL: _HELM_MANAGER,
            _COMPONENT_LABEL: "container-worker",
            _POOL_LABEL_KEY: target.pool_name,
        }
        invalid = [
            key for key, expected in expected_labels.items() if metadata.labels.get(key) != expected
        ]
        if metadata.annotations.get(_SCALING_OWNER_ANNOTATION) != _SCALING_OWNER:
            invalid.append(_SCALING_OWNER_ANNOTATION)
        if metadata.annotations.get(_CAPACITY_OWNER_ANNOTATION) != target.capacity_owner_id:
            invalid.append(_CAPACITY_OWNER_ANNOTATION)
        if metadata.name != target.deployment_name:
            invalid.append("metadata.name")
        if metadata.namespace != target.namespace:
            invalid.append("metadata.namespace")
        if invalid:
            raise KubernetesDeploymentOwnershipError(
                "refusing to scale a Deployment not owned by the Helm container-worker pool: "
                + ", ".join(sorted(invalid))
            )


@dataclass(slots=True)
class KubernetesContainerWorkerReplicaScaler:
    settings: KubernetesContainerWorkerScalerSettings
    scale_api: KubernetesContainerWorkerScaleApi

    @classmethod
    def from_cluster(
        cls,
        settings: KubernetesContainerWorkerScalerSettings,
    ) -> KubernetesContainerWorkerReplicaScaler:
        return cls(settings, KubernetesAppsScaleApi.from_cluster())

    def scale_pool(
        self,
        pool_name: str,
        replicas: int,
        *,
        minimum_replicas: int,
        maximum_replicas: int,
        capacity_owner_id: str,
        reservation_id: str,
        operation_id: str,
    ) -> KubernetesContainerWorkerScaleResult:
        target = container_worker_scale_target(
            self.settings,
            pool_name,
            replicas,
            minimum_replicas=minimum_replicas,
            maximum_replicas=maximum_replicas,
            capacity_owner_id=capacity_owner_id,
            reservation_id=reservation_id,
            operation_id=operation_id,
        )
        if not minimum_replicas <= replicas <= maximum_replicas:
            return KubernetesContainerWorkerScaleResult(
                outcome=KubernetesContainerWorkerScaleOutcome.AtLimit,
                target=target,
                reason=(
                    f"desired replicas {replicas} are outside the durable pool bounds "
                    f"{minimum_replicas}..{maximum_replicas}"
                ),
            )
        try:
            state = self.scale_api.describe_helm_container_worker(target)
            if state.desired_replicas == replicas:
                return KubernetesContainerWorkerScaleResult(
                    outcome=KubernetesContainerWorkerScaleOutcome.ExistingPending,
                    target=target,
                    state=state,
                    reason="the authoritative Deployment already has the requested desired state",
                )
            updated = self.scale_api.compare_and_set_helm_container_worker(
                target,
                resource_version=state.resource_version,
            )
        except KubernetesDeploymentOwnershipError as exc:
            return KubernetesContainerWorkerScaleResult(
                outcome=KubernetesContainerWorkerScaleOutcome.Unsupported,
                target=target,
                reason=str(exc),
            )
        except ApiException as exc:
            return self._api_failure(target, exc)
        if updated.desired_replicas != replicas:
            return KubernetesContainerWorkerScaleResult(
                outcome=KubernetesContainerWorkerScaleOutcome.TemporarilyUnavailable,
                target=target,
                state=updated,
                retry_after_seconds=1.0,
                reason="Kubernetes did not retain the requested Deployment desired state",
            )
        return KubernetesContainerWorkerScaleResult(
            outcome=KubernetesContainerWorkerScaleOutcome.Requested,
            target=target,
            state=updated,
            reason="Kubernetes accepted the bounded Deployment desired state",
        )

    def describe_pool(
        self,
        pool_name: str,
        *,
        minimum_replicas: int,
        maximum_replicas: int,
        capacity_owner_id: str,
        reservation_id: str,
        operation_id: str,
    ) -> KubernetesContainerWorkerScaleResult:
        target = container_worker_scale_target(
            self.settings,
            pool_name,
            minimum_replicas,
            minimum_replicas=minimum_replicas,
            maximum_replicas=maximum_replicas,
            capacity_owner_id=capacity_owner_id,
            reservation_id=reservation_id,
            operation_id=operation_id,
        )
        try:
            state = self.scale_api.describe_helm_container_worker(target)
        except KubernetesDeploymentOwnershipError as exc:
            return KubernetesContainerWorkerScaleResult(
                outcome=KubernetesContainerWorkerScaleOutcome.Unsupported,
                target=target,
                reason=str(exc),
            )
        except ApiException as exc:
            return self._api_failure(target, exc)
        return KubernetesContainerWorkerScaleResult(
            outcome=KubernetesContainerWorkerScaleOutcome.ExistingPending,
            target=target.model_copy(update={"desired_replicas": state.desired_replicas}),
            state=state,
            reason="described authoritative Kubernetes Deployment desired state",
        )

    @staticmethod
    def _api_failure(
        target: KubernetesContainerWorkerScaleTarget,
        error: ApiException,
    ) -> KubernetesContainerWorkerScaleResult:
        error_value: object = error
        if not _has_kubernetes_api_status(error_value):
            raise RuntimeError("Kubernetes API error has an invalid status") from error
        status = error_value.status
        if status in {403, 404}:
            outcome = KubernetesContainerWorkerScaleOutcome.Unsupported
            retry_after_seconds = None
        elif status in {409, 429} or status is None or status >= 500:
            outcome = KubernetesContainerWorkerScaleOutcome.TemporarilyUnavailable
            retry_after_seconds = 1.0
        else:
            outcome = KubernetesContainerWorkerScaleOutcome.Unsupported
            retry_after_seconds = None
        status_label = "unknown" if status is None else str(status)
        return KubernetesContainerWorkerScaleResult(
            outcome=outcome,
            target=target,
            retry_after_seconds=retry_after_seconds,
            reason=f"Kubernetes scale request failed with API status {status_label}",
        )


def container_worker_scale_target(
    settings: KubernetesContainerWorkerScalerSettings,
    pool_name: str,
    replicas: int,
    *,
    minimum_replicas: int,
    maximum_replicas: int,
    capacity_owner_id: str,
    reservation_id: str,
    operation_id: str,
) -> KubernetesContainerWorkerScaleTarget:
    normalized_pool = pool_name.strip()
    if len(normalized_pool) > 63 or _POOL_LABEL.fullmatch(normalized_pool) is None:
        raise ValueError("worker pool name must be a Kubernetes label value")
    pool_key = normalized_pool.lower().replace("_", "-")
    untruncated_name = f"{settings.deployment_prefix}-container-worker-{pool_key}"
    deployment_name = untruncated_name[:63].rstrip("-")
    if not deployment_name.endswith(pool_key):
        raise ValueError("Helm deployment name would truncate the worker pool identity")
    return KubernetesContainerWorkerScaleTarget(
        capacity_owner_id=capacity_owner_id,
        pool_name=normalized_pool,
        namespace=settings.namespace,
        deployment_name=deployment_name,
        desired_replicas=replicas,
        minimum_replicas=minimum_replicas,
        maximum_replicas=maximum_replicas,
        reservation_id=reservation_id,
        operation_id=operation_id,
    )


__all__ = [
    "KubernetesAppsScaleApi",
    "KubernetesContainerWorkerDeploymentState",
    "KubernetesContainerWorkerReplicaScaler",
    "KubernetesContainerWorkerScaleApi",
    "KubernetesContainerWorkerScaleOutcome",
    "KubernetesContainerWorkerScaleResult",
    "KubernetesContainerWorkerScaleTarget",
    "KubernetesContainerWorkerScalerSettings",
    "KubernetesDeploymentOwnershipError",
    "container_worker_scale_target",
]

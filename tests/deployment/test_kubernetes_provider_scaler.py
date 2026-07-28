from __future__ import annotations

from dataclasses import dataclass, field

import provider_kubernetes.provider as provider_module
import pytest
from kubernetes.client.exceptions import ApiException
from provider_kubernetes import (
    KubernetesAppsScaleApi,
    KubernetesContainerWorkerDeploymentState,
    KubernetesContainerWorkerReplicaScaler,
    KubernetesContainerWorkerScaleOutcome,
    KubernetesContainerWorkerScalerSettings,
    KubernetesContainerWorkerScaleTarget,
    KubernetesDeploymentOwnershipError,
    container_worker_scale_target,
)
from pydantic import JsonValue, ValidationError

_CAPACITY_OWNER_ID = "6fb19db5-ddd0-478d-8f4a-cdf422ad438c"


def _target(
    replicas: int = 3,
    *,
    minimum_replicas: int = 1,
    maximum_replicas: int = 4,
) -> KubernetesContainerWorkerScaleTarget:
    return container_worker_scale_target(
        KubernetesContainerWorkerScalerSettings(),
        "default",
        replicas,
        minimum_replicas=minimum_replicas,
        maximum_replicas=maximum_replicas,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )


def _state(
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


@dataclass(slots=True)
class _ScalePort:
    state: KubernetesContainerWorkerDeploymentState
    updated: KubernetesContainerWorkerDeploymentState
    describe_error: Exception | None = None
    update_error: Exception | None = None
    describes: list[KubernetesContainerWorkerScaleTarget] = field(default_factory=list)
    updates: list[tuple[KubernetesContainerWorkerScaleTarget, str]] = field(default_factory=list)

    def describe_helm_container_worker(
        self,
        target: KubernetesContainerWorkerScaleTarget,
    ) -> KubernetesContainerWorkerDeploymentState:
        self.describes.append(target)
        if self.describe_error is not None:
            raise self.describe_error
        return self.state

    def compare_and_set_helm_container_worker(
        self,
        target: KubernetesContainerWorkerScaleTarget,
        *,
        resource_version: str,
    ) -> KubernetesContainerWorkerDeploymentState:
        self.updates.append((target, resource_version))
        if self.update_error is not None:
            raise self.update_error
        return self.updated


def test_replica_scaler_requests_bounded_desired_state_with_resource_version() -> None:
    target = _target()
    initial = _state(target, desired_replicas=2, observed_replicas=2, resource_version="41")
    updated = _state(target, desired_replicas=3, observed_replicas=2, resource_version="42")
    scale_api = _ScalePort(initial, updated)
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        3,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is KubernetesContainerWorkerScaleOutcome.Requested
    assert result.state == updated
    assert result.target.capacity_owner_id == _CAPACITY_OWNER_ID
    assert scale_api.describes == [result.target]
    assert scale_api.updates == [(result.target, "41")]


def test_replica_scaler_reuses_authoritative_desired_state_without_another_patch() -> None:
    target = _target()
    pending = _state(target, desired_replicas=3, observed_replicas=1, resource_version="42")
    scale_api = _ScalePort(pending, pending)
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        3,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is KubernetesContainerWorkerScaleOutcome.ExistingPending
    assert result.state == pending
    assert result.state is not None
    assert result.state.observed_replicas == 1
    assert scale_api.updates == []


@pytest.mark.parametrize("replicas", [0, 5])
def test_replica_scaler_reports_at_limit_without_touching_kubernetes(replicas: int) -> None:
    target = _target(replicas)
    state = _state(target, desired_replicas=2, observed_replicas=2, resource_version="41")
    scale_api = _ScalePort(state, state)
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        replicas,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is KubernetesContainerWorkerScaleOutcome.AtLimit
    assert result.state is None
    assert scale_api.describes == []
    assert scale_api.updates == []


@pytest.mark.parametrize(
    ("error", "outcome", "retry_after"),
    [
        (ApiException(status=404), KubernetesContainerWorkerScaleOutcome.Unsupported, None),
        (
            ApiException(status=409),
            KubernetesContainerWorkerScaleOutcome.TemporarilyUnavailable,
            1.0,
        ),
        (
            ApiException(status=503),
            KubernetesContainerWorkerScaleOutcome.TemporarilyUnavailable,
            1.0,
        ),
    ],
)
def test_replica_scaler_classifies_owned_kubernetes_failures(
    error: ApiException,
    outcome: KubernetesContainerWorkerScaleOutcome,
    retry_after: float | None,
) -> None:
    target = _target()
    state = _state(target, desired_replicas=2, observed_replicas=2, resource_version="41")
    scale_api = _ScalePort(state, state, describe_error=error)
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        3,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is outcome
    assert result.retry_after_seconds == retry_after
    assert result.state is None


def test_replica_scaler_classifies_wrongly_owned_deployment_as_unsupported() -> None:
    target = _target()
    state = _state(target, desired_replicas=2, observed_replicas=2, resource_version="41")
    scale_api = _ScalePort(
        state,
        state,
        describe_error=KubernetesDeploymentOwnershipError("wrong scaling owner"),
    )
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        3,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is KubernetesContainerWorkerScaleOutcome.Unsupported
    assert result.reason == "wrong scaling owner"


def test_replica_scaler_treats_cas_conflict_as_retryable_without_reporting_success() -> None:
    target = _target()
    state = _state(target, desired_replicas=2, observed_replicas=2, resource_version="41")
    scale_api = _ScalePort(state, state, update_error=ApiException(status=409))
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        3,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is KubernetesContainerWorkerScaleOutcome.TemporarilyUnavailable
    assert result.state is None
    assert scale_api.updates == [(result.target, "41")]


def test_replica_scaler_rejects_scale_response_that_did_not_retain_target() -> None:
    target = _target()
    initial = _state(target, desired_replicas=2, observed_replicas=2, resource_version="41")
    changed = _state(target, desired_replicas=4, observed_replicas=2, resource_version="43")
    scale_api = _ScalePort(initial, changed)
    scaler = KubernetesContainerWorkerReplicaScaler(
        KubernetesContainerWorkerScalerSettings(), scale_api
    )

    result = scaler.scale_pool(
        "default",
        3,
        minimum_replicas=1,
        maximum_replicas=4,
        capacity_owner_id=_CAPACITY_OWNER_ID,
        reservation_id="reservation-1",
        operation_id="operation-1",
    )

    assert result.outcome is KubernetesContainerWorkerScaleOutcome.TemporarilyUnavailable
    assert result.state == changed
    assert result.retry_after_seconds == 1.0


def test_scale_target_rejects_invalid_pool_bounds_identity_and_ambiguous_truncation() -> None:
    settings = KubernetesContainerWorkerScalerSettings()

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        _target(-1)
    with pytest.raises(ValueError, match="Kubernetes label value"):
        container_worker_scale_target(
            settings,
            "invalid/pool",
            1,
            minimum_replicas=0,
            maximum_replicas=2,
            capacity_owner_id=_CAPACITY_OWNER_ID,
            reservation_id="reservation-1",
            operation_id="operation-1",
        )
    with pytest.raises(ValueError, match="truncate the worker pool identity"):
        container_worker_scale_target(
            KubernetesContainerWorkerScalerSettings(deployment_prefix="a" * 50),
            "default",
            1,
            minimum_replicas=0,
            maximum_replicas=2,
            capacity_owner_id=_CAPACITY_OWNER_ID,
            reservation_id="reservation-1",
            operation_id="operation-1",
        )
    with pytest.raises(ValidationError, match="maximum replicas"):
        container_worker_scale_target(
            settings,
            "default",
            1,
            minimum_replicas=2,
            maximum_replicas=1,
            capacity_owner_id=_CAPACITY_OWNER_ID,
            reservation_id="reservation-1",
            operation_id="operation-1",
        )
    with pytest.raises(ValidationError, match="at least 1 character"):
        container_worker_scale_target(
            settings,
            "default",
            1,
            minimum_replicas=0,
            maximum_replicas=2,
            capacity_owner_id=_CAPACITY_OWNER_ID,
            reservation_id=" ",
            operation_id="operation-1",
        )
    invalid_owner = _target().model_dump()
    invalid_owner["capacity_owner_id"] = "not-a-durable-owner-id"
    with pytest.raises(ValidationError, match="String should match pattern"):
        KubernetesContainerWorkerScaleTarget.model_validate(invalid_owner)


@dataclass(frozen=True, slots=True)
class _Response:
    payload: JsonValue


@dataclass(slots=True)
class _Serializer:
    def sanitize_for_serialization(self, response: _Response) -> JsonValue:
        return response.payload


@dataclass(slots=True)
class _RawAppsApi:
    deployment: JsonValue
    scale: JsonValue
    read_error: Exception | None = None
    patch_error: Exception | None = None
    reads: list[tuple[str, str]] = field(default_factory=list)
    patches: list[dict[str, JsonValue]] = field(default_factory=list)

    def read_namespaced_deployment(self, *, name: str, namespace: str) -> _Response:
        self.reads.append((name, namespace))
        if self.read_error is not None:
            raise self.read_error
        return _Response(self.deployment)

    def patch_namespaced_deployment_scale(
        self,
        *,
        name: str,
        namespace: str,
        body: dict[str, JsonValue],
    ) -> _Response:
        self.patches.append({"name": name, "namespace": namespace, "body": body})
        if self.patch_error is not None:
            raise self.patch_error
        return _Response(self.scale)


def _deployment(
    *,
    pool_name: str = "default",
    managed_by: str = "Helm",
    component: str = "container-worker",
    scaling_owner: str = "scheduler-worker-pool-controller",
    capacity_owner_id: str = _CAPACITY_OWNER_ID,
    name: str = "lazycloud-container-worker-default",
    namespace: str = "lazycloud-system",
    desired_replicas: int = 2,
    observed_replicas: int = 1,
) -> JsonValue:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "resourceVersion": "42",
            "labels": {
                "app.kubernetes.io/managed-by": managed_by,
                "app.kubernetes.io/component": component,
                "lazycloud.io/worker-pool": pool_name,
            },
            "annotations": {
                "lazycloud.io/scaling-owner": scaling_owner,
                "lazycloud.io/capacity-owner-id": capacity_owner_id,
            },
        },
        "spec": {"replicas": desired_replicas},
        "status": {"replicas": observed_replicas},
    }


def _scale(*, desired_replicas: int = 3, observed_replicas: int = 2) -> JsonValue:
    return {
        "metadata": {"resourceVersion": "43"},
        "spec": {"replicas": desired_replicas},
        "status": {"replicas": observed_replicas},
    }


def _cluster_api(
    monkeypatch: pytest.MonkeyPatch,
    raw: _RawAppsApi,
) -> KubernetesAppsScaleApi:
    monkeypatch.setattr(provider_module.config, "load_incluster_config", lambda: None)
    monkeypatch.setattr(provider_module.client, "AppsV1Api", lambda: raw)
    monkeypatch.setattr(provider_module.client, "ApiClient", _Serializer)
    monkeypatch.setattr(provider_module.client, "V1Deployment", _Response)
    monkeypatch.setattr(provider_module.client, "V1Scale", _Response)
    return KubernetesAppsScaleApi.from_cluster()


def test_kubernetes_api_describes_authoritative_deployment_and_cas_scales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _RawAppsApi(deployment=_deployment(), scale=_scale())
    api = _cluster_api(monkeypatch, raw)
    target = _target()

    initial = api.describe_helm_container_worker(target)
    updated = api.compare_and_set_helm_container_worker(
        target,
        resource_version=initial.resource_version,
    )

    assert initial.desired_replicas == 2
    assert initial.observed_replicas == 1
    assert initial.resource_version == "42"
    assert updated.desired_replicas == 3
    assert updated.observed_replicas == 2
    assert updated.resource_version == "43"
    assert raw.reads == [(target.deployment_name, target.namespace)]
    assert raw.patches == [
        {
            "name": target.deployment_name,
            "namespace": target.namespace,
            "body": {
                "metadata": {"resourceVersion": "42"},
                "spec": {"replicas": 3},
            },
        }
    ]


@pytest.mark.parametrize(
    ("deployment", "missing_marker"),
    [
        (_deployment(managed_by="operator"), "app.kubernetes.io/managed-by"),
        (_deployment(component="scheduler"), "app.kubernetes.io/component"),
        (_deployment(pool_name="another"), "lazycloud.io/worker-pool"),
        (_deployment(scaling_owner="horizontal-pod-autoscaler"), "lazycloud.io/scaling-owner"),
        (
            _deployment(capacity_owner_id="e7e8b461-ae4f-4764-ad71-7ec839cbcab6"),
            "capacity-owner-id",
        ),
        (_deployment(name="another"), "metadata.name"),
        (_deployment(namespace="another"), "metadata.namespace"),
    ],
)
def test_kubernetes_api_refuses_non_helm_or_wrong_pool_deployment(
    monkeypatch: pytest.MonkeyPatch,
    deployment: JsonValue,
    missing_marker: str,
) -> None:
    raw = _RawAppsApi(deployment=deployment, scale=_scale())
    api = _cluster_api(monkeypatch, raw)

    with pytest.raises(KubernetesDeploymentOwnershipError, match=missing_marker):
        api.describe_helm_container_worker(_target())

    assert raw.patches == []


@pytest.mark.parametrize("payload", [{}, {"metadata": {"resourceVersion": "43"}}])
def test_kubernetes_api_rejects_incomplete_scale_response(
    monkeypatch: pytest.MonkeyPatch,
    payload: JsonValue,
) -> None:
    raw = _RawAppsApi(deployment=_deployment(), scale=payload)
    api = _cluster_api(monkeypatch, raw)

    with pytest.raises(RuntimeError, match="scale response is incomplete"):
        api.compare_and_set_helm_container_worker(_target(), resource_version="42")

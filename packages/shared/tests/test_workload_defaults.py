from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.autoscaling import QueueDepthAutoscaler
from shared.deployment_records import (
    DeploymentSpec,
    Resources,
)
from shared.deployments import DeploymentKind
from shared.http.deployments import DeploymentResourcesResponse
from shared.http.gateway import Autoscaler, GetOrCreateStubRequest
from shared.http.stubs import StubConfigUpdateRequest, StubCreateRequest, StubRuntimeConfigResponse
from shared.workload_config import StubRuntimeConfig


def test_new_workload_defaults_preserve_explicit_and_stored_preemption_choices() -> None:
    created = StubCreateRequest(name="new")
    assert created.config.runtime is not None
    assert created.config.runtime.preemptible is True
    explicit = StubCreateRequest.model_validate(
        {"name": "durable", "config": {"runtime": {"preemptible": False}}}
    )
    assert explicit.config.runtime is not None
    assert explicit.config.runtime.preemptible is False
    assert StubRuntimeConfig.model_validate({}).preemptible is False

    update = StubConfigUpdateRequest.model_validate({"runtime": {"cpu": 2000}})
    assert update.fields() == {"runtime": {"cpu": 2000}}
    clear_pin = StubConfigUpdateRequest.model_validate(
        {"runtime": {"availability_zone": "", "preemptible": False}}
    )
    assert clear_pin.fields() == {"runtime": {"availability_zone": "", "preemptible": False}}


def test_deployment_concurrency_is_positive_at_public_http_boundaries() -> None:
    omitted = DeploymentSpec(name="omitted")
    request = GetOrCreateStubRequest(name="omitted")
    autoscaler = QueueDepthAutoscaler()

    assert omitted.resources.concurrency == 1
    assert request.concurrent_requests == 1
    assert autoscaler.min_containers == 0
    assert autoscaler.max_containers == 1
    assert autoscaler.tasks_per_container == 1

    with pytest.raises(ValidationError, match="concurrency must be greater than zero"):
        DeploymentSpec(name="invalid", resources=Resources(concurrency=0))
    with pytest.raises(ValidationError, match="greater than 0"):
        GetOrCreateStubRequest(name="invalid", concurrent_requests=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        DeploymentResourcesResponse(concurrency=0)
    with pytest.raises(ValidationError, match="greater than 0"):
        StubRuntimeConfigResponse(concurrency=0)
    with pytest.raises(ValidationError, match="only supported for pod"):
        GetOrCreateStubRequest(name="invalid", keep_warm_seconds=-1)
    with pytest.raises(ValidationError, match="requires max_containers"):
        DeploymentSpec(
            name="contradictory",
            kind=DeploymentKind.Pod,
            resources=Resources(keep_warm=-1),
            metadata={"autoscaler": {"max_containers": 0}},
        )
    with pytest.raises(ValidationError, match="requires max_containers"):
        GetOrCreateStubRequest(
            name="contradictory",
            stub_type=DeploymentKind.Pod.value,
            keep_warm_seconds=-1,
            autoscaler=Autoscaler(max_containers=0),
        )

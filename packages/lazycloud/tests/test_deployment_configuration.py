from __future__ import annotations

import pytest
from lazycloud.session.deployment import _stub_request_from_spec
from pydantic import ValidationError
from shared.http.gateway import GetOrCreateStubRequest
from shared.placement import ProductRegion

from lazycloud import App


def test_sdk_transport_preserves_positive_concurrency_and_resolved_capacity() -> None:
    app = App("deployment_config")

    @app.endpoint(name="parallel", workers=2, concurrency=3)
    def parallel() -> str:
        return "ok"

    spec = parallel.spec()
    request = _stub_request_from_spec(spec, workspace="workspace-1")
    http_round_trip = GetOrCreateStubRequest.model_validate_json(request.model_dump_json())

    assert spec.resources.concurrency == 3
    assert http_round_trip.concurrent_requests == 3
    assert http_round_trip.autoscaler.tasks_per_container == 6


def test_sdk_rejects_zero_concurrency_before_transport() -> None:
    app = App("invalid_deployment_config")

    with pytest.raises(ValidationError):

        @app.endpoint(name="invalid", concurrency=0)
        def invalid() -> str:
            return "no"


def test_configured_function_disk_reaches_gateway_contract() -> None:
    @App("configured_disk").function(disk="1Gi")
    def function() -> str:
        return "ok"

    function.configure(disk="24Gi")
    request = _stub_request_from_spec(function.spec(), workspace="workspace-1")
    round_trip = GetOrCreateStubRequest.model_validate_json(request.model_dump_json())

    assert round_trip.disk == "24Gi"


def test_every_workload_preserves_region_intent_through_the_gateway_contract() -> None:
    app = App("regional_workloads")

    @app.function(region="eu-central")
    def function() -> str:
        return "ok"

    @app.endpoint(region="eu-central")
    def endpoint() -> str:
        return "ok"

    @app.asgi(region="eu-central")
    def asgi() -> str:
        return "ok"

    pod = app.pod(name="pod", region="eu-central")
    sandbox = app.sandbox(name="sandbox", region="eu-central")
    for workload in (function, endpoint, asgi, pod, sandbox):
        spec = workload.spec()
        request = _stub_request_from_spec(spec, workspace="workspace-1")
        round_trip = GetOrCreateStubRequest.model_validate_json(request.model_dump_json())
        assert spec.resources.region is ProductRegion.EuCentral
        assert round_trip.region is ProductRegion.EuCentral

    function.configure(region="us-east")
    assert function.spec().resources.region is ProductRegion.UsEast
    assert app.pod(name="automatic").spec().resources.region is None

    with pytest.raises(ValidationError):
        app.pod(name="conflicting", region="eu-central", pool="private").spec()

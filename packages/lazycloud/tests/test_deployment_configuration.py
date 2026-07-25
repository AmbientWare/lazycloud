from __future__ import annotations

import pytest
from lazycloud.session.deployment import _stub_request_from_spec
from pydantic import ValidationError
from shared.http.gateway import GetOrCreateStubRequest

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

    with pytest.raises(ValidationError, match="concurrency must be greater than zero"):

        @app.endpoint(name="invalid", concurrency=0)
        def invalid() -> str:
            return "no"

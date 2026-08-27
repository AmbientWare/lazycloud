from __future__ import annotations

from contextlib import ExitStack

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.http.pricing import pricing_catalog_response


def test_pricing_catalog_is_public_and_comes_from_the_rate_card(
    isolated_services: ApiServices,
    client_stack: ExitStack,
) -> None:
    client = client_stack.enter_context(TestClient(create_app(isolated_services)))

    response = client.get("/api/v1/pricing")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.json() == pricing_catalog_response().model_dump(mode="json")

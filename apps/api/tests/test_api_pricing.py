from __future__ import annotations

from api.server.services import ApiServices
from fastapi.testclient import TestClient
from shared.http.pricing import PricingCatalogResponse


def test_pricing_catalog_is_public(
    api_runtime: tuple[ApiServices, TestClient],
) -> None:
    _, client = api_runtime

    response = client.get("/api/v1/pricing")

    assert response.status_code == 200
    PricingCatalogResponse.model_validate_json(response.content)

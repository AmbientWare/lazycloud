from __future__ import annotations

from fastapi import APIRouter, Response
from shared.http.pricing import PricingCatalogResponse, pricing_catalog_response

router = APIRouter(prefix="/api/v1/pricing", tags=["billing"])


@router.get(
    "",
    response_model=PricingCatalogResponse,
    operation_id="get_pricing_catalog",
)
def get_pricing_catalog(response: Response) -> PricingCatalogResponse:
    response.headers["Cache-Control"] = "public, max-age=300"
    return pricing_catalog_response()


__all__ = ["router"]

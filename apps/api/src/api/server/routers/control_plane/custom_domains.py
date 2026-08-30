from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from shared.custom_domains import CustomDomain, CustomDomainPhase
from shared.http.custom_domains import (
    CustomDomainListResponse,
    CustomDomainRegisterRequest,
    CustomDomainResponse,
    custom_domain_response,
)

from api.server.auth import read_user, write_user
from api.server.dependencies import current_services
from api.server.services import ApiServices

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_UNSETTLED = {CustomDomainPhase.AwaitingVerification, CustomDomainPhase.Validating}


def _refreshed(services: ApiServices, domain: CustomDomain) -> CustomDomain:
    """Re-read a domain still waiting on the edge, because reading is when it matters.

    Whoever is asking is watching for the certificate, so the answer is worth a round
    trip. A settled domain is returned as recorded, and an unreachable edge returns the
    last state rather than failing the read.
    """

    if domain.phase not in _UNSETTLED:
        return domain
    try:
        return services.custom_domains.refresh(domain)
    except Exception:
        # The read still answers, with the last state rather than an error, but
        # an edge that cannot be reached is not nothing: without the traceback
        # the only symptom is a certificate that never appears to progress.
        LOGGER.exception("custom domain refresh failed, returning last known state: %s", domain.id)
        return domain


@router.post(
    "/api/v1/custom-domains",
    response_model=CustomDomainResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="register_custom_domain",
)
def register_custom_domain(
    request: CustomDomainRegisterRequest,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> CustomDomainResponse:
    domain = services.custom_domains.register(
        request.domain,
        dns_mode=request.dns_mode,
        user_id=user_id,
    )
    return custom_domain_response(domain)


@router.get(
    "/api/v1/custom-domains",
    response_model=CustomDomainListResponse,
    operation_id="list_custom_domains",
)
def list_custom_domains(
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> CustomDomainListResponse:
    domains = services.custom_domains.list(user_id=user_id)
    return CustomDomainListResponse(
        data=[custom_domain_response(_refreshed(services, domain)) for domain in domains]
    )


@router.get(
    "/api/v1/custom-domains/{hostname}",
    response_model=CustomDomainResponse,
    operation_id="get_custom_domain",
)
def get_custom_domain(
    hostname: str,
    user_id: read_user,
    services: ApiServices = Depends(current_services),
) -> CustomDomainResponse:
    domain = services.custom_domains.get(hostname, user_id=user_id)
    return custom_domain_response(_refreshed(services, domain))


@router.delete(
    "/api/v1/custom-domains/{hostname}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="remove_custom_domain",
)
def remove_custom_domain(
    hostname: str,
    user_id: write_user,
    services: ApiServices = Depends(current_services),
) -> Response:
    services.custom_domains.remove(hostname, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

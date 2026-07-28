from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from compute.billing import BillingCreditRequest, BillingDecision, ManagedUsage
from compute.offers import ComputeOffer
from compute.projection import PoolConfig
from fastapi.testclient import TestClient
from identity.auth import AuthService
from shared.compute_fleet import ResourceStatus
from shared.errors import ConflictError, InvalidInputError
from shared.timestamps import utc_now
from tests.provider_fixtures import configure_test_provider


@dataclass(slots=True)
class BillingRecorder:
    balance: BillingDecision = field(default_factory=lambda: BillingDecision(ok=True))
    launch_requests: list[BillingCreditRequest] = field(default_factory=list)
    usage: list[ManagedUsage] = field(default_factory=list)

    def check_launch_credit(self, request: BillingCreditRequest) -> BillingDecision:
        self.launch_requests.append(request)
        return BillingDecision(ok=True, available_cents=1000, required_cents=0)

    def check_balance(self, workspace_id: str) -> BillingDecision:
        del workspace_id
        return self.balance

    def record_usage(self, usage: ManagedUsage) -> None:
        self.usage.append(usage)


def test_compute_launch_reconcile_billing_and_termination(isolated_services: ApiServices) -> None:
    offer = ComputeOffer(
        id="gpu-a10",
        provider="generic",
        instance_type="gpu-a10",
        region="lab",
        cpu_millicores=8000,
        memory_mb=32768,
        gpu="A10G",
        gpu_count=1,
        node_count=1,
        hourly_cost_micros=1_000_000,
        available=2,
    )
    provider = configure_test_provider(isolated_services, "generic", [offer])
    billing = BillingRecorder()
    isolated_services.compute.billing = billing
    state = isolated_services.compute.launch_pool_capacity(
        PoolConfig(
            name="gpu",
            providers=["generic"],
            gpu=["A10G"],
            nodes=1,
            ttl="1h",
            max_spend=2.0,
        ),
        owner_token_id="owner-token",
    )

    assert state.name == "gpu"
    assert state.reserved_nodes == 1
    assert state.reservations[0].provider == "generic"
    assert billing.launch_requests[0].quantity == 1
    assert provider.list_machines("gpu")[0].machine_id == state.reservations[0].machine_id

    # Both reconciles stay inside the bootstrap phase deadline so the terminal
    # status below is attributable to credit exhaustion, not bootstrap reclaim.
    later = utc_now() + timedelta(minutes=2)
    reconciled = isolated_services.compute.reconcile_provider_capacity(now=later)[0]
    assert reconciled.reservations[0].status in {"active", "pending"}
    assert billing.usage
    assert billing.usage[0].duration_seconds > 0

    billing.balance = BillingDecision(
        ok=False,
        error_code="insufficient_credits",
        message="credits exhausted",
    )
    terminated = isolated_services.compute.reconcile_provider_capacity(
        now=later + timedelta(minutes=1)
    )[0]
    assert terminated.reservations[0].status == "deleted"
    assert provider.list_machines("gpu") == []
    assert all(
        machine.status is ResourceStatus.Deleted
        for machine in isolated_services.compute.list_machines()
    )


def test_compute_rejects_unavailable_provider_and_mixed_gpu_pool(
    isolated_services: ApiServices,
) -> None:
    try:
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="gpu",
                providers=["missing"],
                gpu=["A10G"],
                nodes=1,
                ttl="1h",
                max_spend=1.0,
            )
        )
    except ConflictError as exc:
        assert "provider_unavailable" in str(exc)
    else:
        raise AssertionError("expected missing provider to be rejected")

    configure_test_provider(isolated_services, "generic", [])
    try:
        isolated_services.compute.launch_pool_capacity(
            PoolConfig(
                name="mixed",
                providers=["generic"],
                gpu=["A10G", "H100"],
                nodes=1,
                ttl="1h",
                max_spend=1.0,
            )
        )
    except InvalidInputError as exc:
        assert "single GPU type" in str(exc)
    else:
        raise AssertionError("expected mixed GPU launch to be rejected")


def test_gateway_maps_provider_offer_discovery_failure_to_503(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = configure_test_provider(isolated_services, "failing", [])

    def fail_offer_discovery() -> list[ComputeOffer]:
        raise RuntimeError("provider endpoint unavailable")

    monkeypatch.setattr(provider, "list_offers", fail_offer_discovery)
    raw_token, token = AuthService(isolated_services.context).create_token("provider-failure")

    with TestClient(create_app(isolated_services)) as client:
        try:
            response = client.get(
                "/api/v1/pools/provider-failure/offers?provider=failing",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        finally:
            AuthService(isolated_services.context).revoke_token(token.id)

    assert response.status_code == 503
    assert response.json()["detail"] == "compute provider 'failing' offer discovery failed"


def test_gateway_rejects_offer_discovery_without_configured_providers(
    isolated_services: ApiServices,
) -> None:
    raw_token, token = AuthService(isolated_services.context).create_token("provider-empty")

    with TestClient(create_app(isolated_services)) as client:
        try:
            response = client.get(
                "/api/v1/pools/provider-empty/offers",
                headers={"Authorization": f"Bearer {raw_token}"},
            )
        finally:
            AuthService(isolated_services.context).revoke_token(token.id)

    assert response.status_code == 503
    assert response.json()["detail"] == "no compute providers are configured"

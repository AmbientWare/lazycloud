import httpx
import pytest
from api.server.services import ApiServices
from provider_hyperstack.client import HyperstackClient
from provider_hyperstack.pooled_provider import HyperstackDeployment, HyperstackPooledProvider
from pydantic import SecretStr


def test_supplier_quote_charges_every_gpu_and_attached_public_ip(
    isolated_services: ApiServices,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        del transport
        if request.url.path == "/v1/pricebook":
            return httpx.Response(
                200,
                json=[
                    {"name": "A100-80G-SXM4", "value": "1.60"},
                    {"name": "PublicIP", "value": "0.00672043"},
                    {"name": "vCPU", "value": "0"},
                    {"name": "RAM", "value": "0"},
                    {"name": "hypervisor-local-storage", "value": "0"},
                ],
            )
        if request.url.path == "/v1/core/flavors":
            return httpx.Response(
                200,
                json={
                    "status": True,
                    "data": [
                        {
                            "flavors": [
                                {
                                    "name": "n3-A100-SXM4x8",
                                    "region_name": "US-1",
                                    "stock_available": True,
                                    "cpu": 192,
                                    "ram": 960,
                                    "disk": 100,
                                    "ephemeral": 16000,
                                    "gpu": "A100-80G-SXM4",
                                    "gpu_count": 8,
                                }
                            ]
                        }
                    ],
                },
            )
        raise AssertionError("unexpected provider request")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    provider = HyperstackPooledProvider(
        provider_ref="hyperstack:test",
        client=HyperstackClient(SecretStr("test")),
        deployments_by_region={
            "US-1": HyperstackDeployment(
                environment_name="test",
                keypair_name="test",
                image_name="published-node",
                recipe_sha256="a" * 64,
            )
        },
        launch_credentials=isolated_services.provider_node_launches,
    )
    (offer,) = provider.list_offers(root_volume_gib=100)
    assert offer.cost_terms.complete_hourly_cost_micros == 12_806_721
    assert offer.cost_terms.compute_hourly_micros == 12_800_000
    assert offer.cost_terms.root_disk_hourly_micros == 0

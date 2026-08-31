from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from provider_pangolin.client import PangolinClient
from provider_pangolin.network import PangolinPrivateNetworkControl
from provider_pangolin.platform import (
    PangolinPlatformBootstrap,
    PangolinPlatformClientCredential,
    PangolinPlatformCredentials,
    PangolinPlatformSiteCredential,
)
from pydantic import JsonValue, SecretStr
from shared.errors import InvalidInputError


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> PangolinClient:
    def with_platform_records(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/site/23":
            return _response(
                request,
                {
                    "siteId": 23,
                    "niceId": "platform-site",
                    "name": "lazycloud-platform-0",
                    "newtId": "platform-newt",
                },
            )
        if request.url.path == "/v1/site/24":
            return _response(
                request,
                {
                    "siteId": 24,
                    "niceId": "platform-site-two",
                    "name": "lazycloud-platform-1",
                    "newtId": "platform-newt-two",
                },
            )
        if request.url.path == "/v1/client/17":
            return _response(
                request,
                {
                    "clientId": 17,
                    "niceId": "platform-client",
                    "name": "lazycloud-platform-0",
                    "olmId": "platform-client",
                },
            )
        if request.url.path == "/v1/client/18":
            return _response(
                request,
                {
                    "clientId": 18,
                    "niceId": "platform-client-two",
                    "name": "lazycloud-platform-1",
                    "olmId": "platform-client-two",
                },
            )
        return handler(request)

    return PangolinClient(
        api_url="http://pangolin:3003/v1",
        api_key=SecretStr("integration-key"),
        organization_id="org-one",
        endpoint="https://pangolin.example",
        platform_site_ids=(23, 24),
        platform_client_record_ids=(17, 18),
        platform_target_host="control-plane",
        platform_target_port=9000,
        transport=httpx.MockTransport(with_platform_records),
    )


def _response(
    request: httpx.Request,
    data: object,
    *,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        request=request,
        json={
            "data": data,
            "success": True,
            "error": False,
            "message": "ok",
            "status": status_code,
        },
    )


def test_site_credential_uses_the_integration_api_site_contract() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/org/org-one/site"
        assert json.loads(request.content) == {
            "name": "lazycloud-machine-g1",
            "type": "newt",
        }
        return _response(
            request,
            {
                "siteId": 41,
                "niceId": "site-one",
                "name": "lazycloud-machine-g1",
                "newtId": "newt-one",
                "secret": "newt-secret",
            },
            status_code=201,
        )

    credential = PangolinPrivateNetworkControl(_client(handle)).create_site(
        name="lazycloud-machine-g1"
    )

    assert credential.site_id == "41"
    assert credential.connector_id == "newt-one"
    assert credential.secret.get_secret_value() == "newt-secret"


def test_private_resource_grants_only_the_platform_machine_client() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return _response(request, {"siteResources": []})
        return _response(
            request,
            {
                "siteResourceId": 91,
                "name": "lazycloud-site-41-route-proxy",
                "aliasAddress": "100.89.0.8",
                "mode": "host",
                "destination": "127.0.0.1",
                "tcpPortRangeString": "29443",
                "udpPortRangeString": "",
            },
        )

    resource = _client(handle).ensure_agent_private_resource(
        site_id=41,
        destination_port=29443,
    )

    assert resource.alias_address == "100.89.0.8"
    assert requests[0].url.path == "/v1/org/org-one/private-resources"
    assert requests[1].url.path == "/v1/org/org-one/private-resource"
    body = json.loads(requests[1].content)
    assert body["siteIds"] == [41]
    assert body["clientIds"] == [17, 18]
    assert body["userIds"] == []
    assert body["roleIds"] == []


def test_existing_private_resource_rejects_unexpected_machine_access() -> None:
    resources: list[JsonValue] = [
        {
            "siteResourceId": 91,
            "name": "lazycloud-site-41-route-proxy",
            "aliasAddress": "100.89.0.8",
            "mode": "host",
            "destination": "127.0.0.1",
            "tcpPortRangeString": "29443",
            "udpPortRangeString": "",
            "siteIds": [41],
        }
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/private-resources"):
            return _response(request, {"siteResources": resources})
        assert request.url.path == "/v1/private-resource/91/clients"
        return _response(request, {"clients": [{"clientId": 17}]})

    with pytest.raises(InvalidInputError, match="unexpected machine access"):
        _client(handle).ensure_agent_private_resource(
            site_id=41,
            destination_port=29443,
        )


def test_platform_client_scaling_replaces_private_resource_access() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/org/org-one/private-resources":
            response_data: dict[str, JsonValue] = {
                "siteResources": [
                    {
                        "siteResourceId": 91,
                        "name": "lazycloud-site-41-route-proxy",
                        "aliasAddress": "100.89.0.8",
                        "mode": "host",
                        "destination": "127.0.0.1",
                        "tcpPortRangeString": "29443",
                        "udpPortRangeString": "",
                        "siteIds": [41],
                    }
                ]
            }
            return _response(
                request,
                response_data,
            )
        if request.method == "GET":
            return _response(request, {"clients": [{"clientId": 17}]})
        return _response(request, {}, status_code=201)

    _client(handle).synchronize_platform_client_access((17, 18))

    assert requests[-1].url.path == "/v1/private-resource/91/clients"
    assert json.loads(requests[-1].content) == {"clientIds": [17, 18]}


def test_site_cleanup_deletes_its_private_resource_before_the_site() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            response_data: dict[str, JsonValue] = {
                "siteResources": [
                    {
                        "siteResourceId": 91,
                        "name": "lazycloud-site-41-route-proxy",
                        "aliasAddress": "100.89.0.8",
                        "mode": "host",
                        "destination": "127.0.0.1",
                        "tcpPortRangeString": "29443",
                        "udpPortRangeString": "",
                    }
                ]
            }
            return _response(
                request,
                response_data,
            )
        return _response(request, None)

    _client(handle).delete_site(41)

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/v1/org/org-one/private-resources"),
        ("DELETE", "/v1/private-resource/91"),
        ("DELETE", "/v1/site/41"),
    ]


def test_domain_cleanup_removes_only_lazycloud_owned_resources() -> None:
    deleted: list[str] = []
    resources: list[JsonValue] = [
        {
            "resourceId": 1,
            "domainId": "domain-one",
            "name": "lazycloud-domain-one-apex",
            "fullDomain": "app.example.com",
            "mode": "http",
            "ssl": True,
            "sso": False,
            "enabled": True,
        },
        {
            "resourceId": 2,
            "domainId": "domain-one",
            "name": "customer-owned-resource",
            "fullDomain": "other.example.com",
            "mode": "http",
            "ssl": True,
            "sso": True,
            "enabled": True,
        },
        {
            "resourceId": 3,
            "domainId": "domain-two",
            "name": "lazycloud-domain-two-apex",
            "fullDomain": "third.example.com",
            "mode": "http",
            "ssl": True,
            "sso": False,
            "enabled": True,
        },
    ]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return _response(request, {"resources": resources})
        deleted.append(request.url.path)
        return _response(request, None)

    _client(handle).delete_hostname("cname:domain-one")

    assert deleted == [
        "/v1/public-resource/1",
        "/v1/org/org-one/domain/domain-one",
    ]


def test_platform_public_resource_has_one_health_checked_target_per_site() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/org/org-one/domains":
            domains: JsonValue = {
                "domains": [
                    {
                        "domainId": "platform-domain",
                        "baseDomain": "lazycloud.example.com",
                        "type": None,
                        "verified": True,
                        "configManaged": True,
                    }
                ]
            }
            return _response(
                request,
                domains,
            )
        if request.url.path == "/v1/org/org-one/public-resources":
            return _response(request, {"resources": []})
        if request.url.path == "/v1/org/org-one/public-resource":
            return _response(
                request,
                {
                    "resourceId": 71,
                    "domainId": "platform-domain",
                    "name": "lazycloud-platform-public",
                    "subdomain": "api",
                    "fullDomain": "api.lazycloud.example.com",
                    "mode": "http",
                    "ssl": True,
                    "sso": True,
                    "enabled": True,
                },
                status_code=201,
            )
        if request.url.path == "/v1/public-resource/71" and request.method == "POST":
            return _response(
                request,
                {
                    "resourceId": 71,
                    "domainId": "platform-domain",
                    "name": "lazycloud-platform-public",
                    "subdomain": "api",
                    "fullDomain": "api.lazycloud.example.com",
                    "mode": "http",
                    "ssl": False,
                    "sso": False,
                    "enabled": True,
                },
            )
        if request.url.path == "/v1/public-resource/71/targets" and request.method == "GET":
            return _response(request, {"targets": []})
        return _response(request, None, status_code=201)

    resource = _client(handle).ensure_platform_public_resource(
        hostname="api.lazycloud.example.com",
        site_ids=(23, 24),
        ssl=False,
    )

    assert resource.resource_id == 71
    created_resource = json.loads(requests[2].content)
    assert created_resource == {
        "name": "lazycloud-platform-public",
        "subdomain": "api",
        "domainId": "platform-domain",
        "mode": "http",
    }
    assert requests[3].url.path == "/v1/public-resource/71"
    assert json.loads(requests[3].content) == {
        "sso": False,
        "enabled": True,
        "ssl": False,
    }
    target_requests = requests[5:]
    assert all(request.url.path == "/v1/public-resource/71/target" for request in target_requests)
    target_bodies = [json.loads(request.content) for request in target_requests]
    assert [body["siteId"] for body in target_bodies] == [23, 24]
    assert all(
        body["ip"] == "control-plane"
        and body["port"] == 9000
        and body["hcEnabled"] is True
        and body["hcHostname"] == "control-plane"
        and body["hcPath"] == "/health"
        and body["hcStatus"] == 200
        for body in target_bodies
    )
    assert all("mode" not in body for body in target_bodies)


def test_platform_bootstrap_refuses_to_orphan_privileged_identities() -> None:
    class Store:
        credentials = PangolinPlatformCredentials(
            sites=(
                PangolinPlatformSiteCredential(
                    ordinal=0,
                    site_id=23,
                    newt_id="platform-newt",
                    secret=SecretStr("site-secret-one"),
                ),
                PangolinPlatformSiteCredential(
                    ordinal=1,
                    site_id=24,
                    newt_id="platform-newt-two",
                    secret=SecretStr("site-secret-two"),
                ),
            ),
            clients=(
                PangolinPlatformClientCredential(
                    ordinal=0,
                    client_record_id=17,
                    olm_id="platform-client",
                    secret=SecretStr("client-secret-one"),
                ),
                PangolinPlatformClientCredential(
                    ordinal=1,
                    client_record_id=18,
                    olm_id="platform-client-two",
                    secret=SecretStr("client-secret-two"),
                ),
            ),
        )

        def load(self) -> PangolinPlatformCredentials:
            return self.credentials

        def save(self, credentials: PangolinPlatformCredentials) -> None:
            self.credentials = credentials

    def reject_unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected Pangolin request: {request.method} {request.url.path}")

    with pytest.raises(InvalidInputError, match="site count cannot be reduced"):
        PangolinPlatformBootstrap(_client(reject_unexpected), Store()).ensure(
            site_count=1,
            client_count=2,
            public_hostname="api.lazycloud.example.com",
            public_tls=True,
        )

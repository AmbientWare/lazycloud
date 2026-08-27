from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest
from networking.tailnet_control import (
    TailnetAuthKey,
    TailnetControlError,
    TailnetControlErrorCode,
    TailnetDevice,
    TailnetMachineIdentityReconciler,
    TailscaleTailnetControl,
    TailscaleTailnetControlConfig,
    tailnet_machine_hostname,
)
from pydantic import JsonValue, SecretStr

API_URL = "https://api.tailscale.test"
CLIENT_SECRET = "tskey-client-secret"


def test_issues_only_single_use_durable_tagged_keys_and_caches_oauth_tokens() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v2/oauth/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "tskey-api-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "auth_keys devices:core",
                },
            )
        if request.url.path == "/api/v2/tailnet/-/keys":
            return httpx.Response(
                200,
                json={
                    "id": f"key-{len(requests)}",
                    "key": f"tskey-auth-machine-{len(requests)}",
                    "expires": "2026-07-14T18:05:00Z",
                    "capabilities": {},
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    control = _control(handler)

    first = control.issue_auth_key(machine_id="machine-1", hostname="agent-machine-1")
    second = control.issue_auth_key(machine_id="machine-2", hostname="agent-machine-2")
    runtime_key = control.issue_runtime_auth_key(hostname="control-plane-one")

    assert first.key.get_secret_value().startswith("tskey-auth-machine-")
    assert second.id != first.id
    assert runtime_key.get_secret_value().startswith("tskey-auth-machine-")
    assert sum(request.url.path == "/api/v2/oauth/token" for request in requests) == 2
    token_request = requests[0]
    token_form = parse_qs(token_request.content.decode())
    assert token_form == {
        "grant_type": ["client_credentials"],
        "client_id": ["oauth-client-id"],
        "client_secret": [CLIENT_SECRET],
        "scope": ["auth_keys devices:core"],
        "tags": ["tag:lazycloud-agent"],
    }
    key_request = requests[1]
    assert key_request.headers["authorization"] == "Bearer tskey-api-token"
    assert json.loads(key_request.content) == {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": False,
                    "preauthorized": True,
                    "tags": ["tag:lazycloud-agent"],
                }
            }
        },
        "expirySeconds": 300,
        "description": "machine machine-1 agent-machine-1",
    }
    control_plane_token_form = parse_qs(requests[3].content.decode())
    assert control_plane_token_form["tags"] == ["tag:lazycloud-control-plane"]
    control_plane_key_request = requests[4]
    assert json.loads(control_plane_key_request.content) == {
        "capabilities": {
            "devices": {
                "create": {
                    "reusable": False,
                    "ephemeral": False,
                    "preauthorized": True,
                    "tags": ["tag:lazycloud-control-plane"],
                }
            }
        },
        "expirySeconds": 300,
        "description": "control plane control-plane-one",
    }
    assert CLIENT_SECRET not in repr(control)
    assert first.key.get_secret_value() not in repr(first)


def test_verifies_device_identity_hostname_authorization_and_tag() -> None:
    control = _control(_device_handler())

    device = control.verify_device("node-stable-1", expected_hostname="agent-machine-1")

    assert device.id == "device-1"
    assert device.node_id == "node-stable-1"
    assert device.hostname == "agent-machine-1"
    assert device.name == "agent-machine-1.example.ts.net"
    assert device.addresses == ("100.64.0.10", "fd7a:115c:a1e0::10")
    assert device.tags == ("tag:lazycloud-agent",)
    assert device.authorized is True
    assert device.created_at is not None
    assert device.last_seen_at is not None


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("hostname", "other-machine", "hostname"),
        ("tags", ["tag:other"], "tag"),
        ("authorized", False, "not authorized"),
    ],
)
def test_rejects_device_that_does_not_match_machine_identity(
    field: str,
    value: JsonValue,
    message: str,
) -> None:
    control = _control(_device_handler(overrides={field: value}))

    with pytest.raises(TailnetControlError, match=message) as exc:
        control.verify_device("node-stable-1", expected_hostname="agent-machine-1")

    assert exc.value.code is TailnetControlErrorCode.VerificationFailed
    assert exc.value.retryable is False


def test_rejects_missing_and_duplicate_stable_node_ids() -> None:
    def missing_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth/token":
            return _token_response()
        if request.url.path == "/api/v2/tailnet/-/devices":
            return httpx.Response(200, json={"devices": [_device_payload()]})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    with pytest.raises(TailnetControlError, match="not found") as missing:
        _control(missing_handler).verify_device(
            "node-stable-missing",
            expected_hostname="agent-machine-1",
        )
    assert missing.value.code is TailnetControlErrorCode.NotFound

    def duplicate_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth/token":
            return _token_response()
        if request.url.path == "/api/v2/tailnet/-/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        _device_payload(),
                        _device_payload(id="device-duplicate"),
                    ]
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    with pytest.raises(TailnetControlError, match="multiple") as duplicate:
        _control(duplicate_handler).verify_device(
            "node-stable-1",
            expected_hostname="agent-machine-1",
        )
    assert duplicate.value.code is TailnetControlErrorCode.Conflict


def test_finds_only_exact_tagged_generations_for_one_machine() -> None:
    machine_id = "33333333-3333-4333-8333-333333333333"
    first = tailnet_machine_hostname(machine_id, 1)
    second = tailnet_machine_hostname(machine_id, 2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth/token":
            return _token_response()
        if request.url.path == "/api/v2/tailnet/-/devices":
            return httpx.Response(
                200,
                json={
                    "devices": [
                        _device_payload(id="first", hostname=first, name=f"{first}.example.ts.net"),
                        _device_payload(
                            id="second",
                            hostname=second,
                            name=f"{second}.example.ts.net",
                        ),
                        _device_payload(
                            id="wrong-tag",
                            hostname=first,
                            tags=["tag:other"],
                        ),
                        _device_payload(
                            id="other-machine",
                            hostname=tailnet_machine_hostname(
                                "44444444-4444-4444-8444-444444444444",
                                1,
                            ),
                        ),
                    ]
                },
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    devices = _control(handler).find_machine_devices(machine_id=machine_id)

    assert [device.id for device in devices] == ["first", "second"]


def test_revoke_and_remove_are_idempotent_when_resources_are_absent() -> None:
    deleted_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth/token":
            return _token_response()
        if request.method == "DELETE":
            deleted_paths.append(request.url.raw_path.decode())
            return httpx.Response(404)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    control = _control(handler)
    control.revoke_auth_key("key/one")
    control.remove_device("device/one")

    assert deleted_paths == [
        "/api/v2/tailnet/-/keys/key%2Fone",
        "/api/v2/device/device%2Fone",
    ]


def test_machine_cleanup_revokes_key_before_exact_generation_reconciliation() -> None:
    machine_id = "machine-1"
    first_hostname = tailnet_machine_hostname(machine_id, 1)
    second_hostname = tailnet_machine_hostname(machine_id, 2)
    control = _CleanupControl(
        devices={
            "known": TailnetDevice(
                id="known",
                node_id="known-node",
                hostname=first_hostname,
                authorized=True,
            ),
            "uncommitted": TailnetDevice(
                id="uncommitted",
                node_id="uncommitted-node",
                hostname=first_hostname,
                authorized=True,
            ),
            "current": TailnetDevice(
                id="current",
                node_id="current-node",
                hostname=second_hostname,
                authorized=True,
            ),
        }
    )
    reconciler = TailnetMachineIdentityReconciler(control)

    reconciler.cleanup(
        machine_id=machine_id,
        generations=(1,),
        auth_key_ids=("key-1", "key-1"),
        device_ids=("known",),
    )
    reconciler.cleanup(
        machine_id=machine_id,
        generations=(1,),
        auth_key_ids=("key-1",),
        device_ids=("known",),
    )

    assert control.events[:4] == [
        ("revoke", "key-1"),
        ("remove", "known"),
        ("find", first_hostname),
        ("remove", "uncommitted"),
    ]
    assert tuple(control.devices) == ("current",)
    assert all(
        value != second_hostname for operation, value in control.events if operation == "find"
    )


def test_refreshes_oauth_token_once_after_api_authentication_failure() -> None:
    token_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_count
        if request.url.path == "/api/v2/oauth/token":
            token_count += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"tskey-api-token-{token_count}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if request.url.path == "/api/v2/device/device-1":
            assert request.url.params.get("fields") == "all"
            if request.headers["authorization"] == "Bearer tskey-api-token-1":
                return httpx.Response(401)
            return httpx.Response(200, json=_device_payload())
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    device = _control(handler).get_device("device-1")

    assert device.id == "device-1"
    assert token_count == 2


@pytest.mark.parametrize(
    ("status_code", "code", "retryable"),
    [
        (403, TailnetControlErrorCode.PermissionDenied, False),
        (404, TailnetControlErrorCode.NotFound, False),
        (429, TailnetControlErrorCode.RateLimited, True),
        (503, TailnetControlErrorCode.UpstreamUnavailable, True),
    ],
)
def test_classifies_upstream_failures(
    status_code: int,
    code: TailnetControlErrorCode,
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth/token":
            return _token_response()
        return httpx.Response(status_code, content=CLIENT_SECRET)

    with pytest.raises(TailnetControlError) as exc:
        _control(handler).get_device("device-1")

    assert exc.value.code is code
    assert exc.value.retryable is retryable
    assert CLIENT_SECRET not in str(exc.value)


def _control(
    handler: Callable[[httpx.Request], httpx.Response] | httpx.MockTransport,
) -> TailscaleTailnetControl:
    transport = (
        handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)
    )
    return TailscaleTailnetControl(
        TailscaleTailnetControlConfig(
            api_url=API_URL,
            oauth_client_id="oauth-client-id",
            oauth_client_secret=SecretStr(CLIENT_SECRET),
            agent_tag="tag:lazycloud-agent",
            control_plane_tag="tag:lazycloud-control-plane",
            auth_key_ttl_seconds=300,
        ),
        transport=transport,
    )


class _CleanupControl:
    def __init__(self, *, devices: dict[str, TailnetDevice]) -> None:
        self.devices = devices
        self.events: list[tuple[str, str]] = []

    def issue_auth_key(self, *, machine_id: str, hostname: str) -> TailnetAuthKey:
        raise AssertionError((machine_id, hostname))

    def revoke_auth_key(self, key_id: str) -> None:
        self.events.append(("revoke", key_id))

    def verify_device(self, node_id: str, *, expected_hostname: str) -> TailnetDevice:
        raise AssertionError((node_id, expected_hostname))

    def find_devices(self, *, hostname: str) -> tuple[TailnetDevice, ...]:
        self.events.append(("find", hostname))
        return tuple(device for device in self.devices.values() if device.hostname == hostname)

    def remove_device(self, device_id: str) -> None:
        self.events.append(("remove", device_id))
        self.devices.pop(device_id, None)


def _token_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "tskey-api-token",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )


def _device_handler(
    *,
    overrides: dict[str, JsonValue] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/oauth/token":
            return _token_response()
        if request.url.path == "/api/v2/tailnet/-/devices":
            assert request.url.params.get("fields") == "all"
            return httpx.Response(
                200,
                json={"devices": [_device_payload(**(overrides or {}))]},
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _device_payload(**overrides: JsonValue) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "id": "device-1",
        "nodeId": "node-stable-1",
        "hostname": "agent-machine-1",
        "name": "agent-machine-1.example.ts.net",
        "addresses": ["100.64.0.10", "fd7a:115c:a1e0::10"],
        "tags": ["tag:lazycloud-agent"],
        "authorized": True,
        "created": "2026-07-14T17:00:00Z",
        "lastSeen": "2026-07-14T17:05:00Z",
        "clientVersion": "1.98.4",
    }
    payload.update(overrides)
    return payload

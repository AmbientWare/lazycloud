from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import ClassVar

import pytest
from compute.billing import (
    BillingCreditRequest,
    HttpManagedComputeBilling,
)
from observability.managed_billing import ManagedBillingMode, ManagedBillingSettings
from pydantic import JsonValue, TypeAdapter

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class _BillingHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str, JsonValue]]] = []
    response_status: ClassVar[int] = 200
    response_body: ClassVar[bytes] = b'{"ok":true,"available_cents":50}'

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = _JSON_OBJECT_ADAPTER.validate_json(self.rfile.read(content_length))
        self.requests.append(
            (
                self.path,
                self.headers.get("Authorization", ""),
                payload,
            )
        )
        self.send_response(self.response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)


def _billing_server() -> tuple[ThreadingHTTPServer, Thread, str]:
    _BillingHandler.requests.clear()
    _BillingHandler.response_status = 200
    _BillingHandler.response_body = b'{"ok":true,"available_cents":50}'
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BillingHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/v1"


def test_http_managed_billing_uses_bounded_typed_production_io() -> None:
    server, thread, endpoint = _billing_server()
    try:
        client = HttpManagedComputeBilling(
            ManagedBillingSettings(
                mode=ManagedBillingMode.Http,
                endpoint=endpoint,
                auth_token="test-billing-token",
                minimum_credit_cents=25,
            )
        )

        result = client.check_launch_credit(
            BillingCreditRequest(workspace_id="workspace-1", pool_name="pool-1")
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.ok is True
    assert result.available_cents == 50
    assert result.required_cents == 25
    assert _BillingHandler.requests == [
        (
            "/v1/launch-check/",
            "Bearer test-billing-token",
            {
                "workspace_id": "workspace-1",
                "pool_name": "pool-1",
                "required_cents": 25,
                "quantity": 0,
                "estimated_hourly_cost_micros": 0,
                "estimated_committed_micros": 0,
            },
        )
    ]


def test_http_managed_billing_surfaces_upstream_status_without_soft_success() -> None:
    server, thread, endpoint = _billing_server()
    _BillingHandler.response_status = 402
    _BillingHandler.response_body = b'{"ok":false,"message":"credit required"}'
    try:
        client = HttpManagedComputeBilling(
            ManagedBillingSettings(mode=ManagedBillingMode.Http, endpoint=endpoint)
        )
        with pytest.raises(RuntimeError, match="status 402"):
            client.check_balance("workspace-1")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

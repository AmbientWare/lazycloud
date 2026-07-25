from __future__ import annotations

import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass

import pytest
from shared.http.errors import HttpTransportError
from shared.http_transport import HttpChannel, build_http_ssl_context


@dataclass(slots=True)
class _NoContentResponse:
    status: int = 204

    def __enter__(self) -> _NoContentResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_http_ssl_context_keeps_strict_verification_and_adds_portable_roots() -> None:
    context = build_http_ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs()


def test_http_channel_uses_its_strict_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_context: ssl.SSLContext | None = None

    def urlopen(
        _request: urllib.request.Request,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> _NoContentResponse:
        nonlocal observed_context
        assert timeout == 3.0
        observed_context = context
        return _NoContentResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    channel = HttpChannel(endpoint="https://control.example.com", timeout_seconds=3.0)

    assert channel.get("/health") is None
    assert observed_context is channel.ssl_context
    assert channel.ssl_context.verify_mode is ssl.CERT_REQUIRED
    assert channel.ssl_context.check_hostname is True


def test_http_channel_uses_the_shared_no_response_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(
        request: urllib.request.Request,
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> _NoContentResponse:
        _ = timeout, context
        raise urllib.error.URLError(ConnectionRefusedError(request.full_url))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    channel = HttpChannel(endpoint="https://control.example.com")

    with pytest.raises(HttpTransportError) as captured:
        channel.get("/health")

    assert captured.value.method == "GET"
    assert captured.value.url == "https://control.example.com/health"

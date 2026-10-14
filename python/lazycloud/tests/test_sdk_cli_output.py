from __future__ import annotations

import email.message
import io
import sys
import urllib.error

import pytest
from lazycloud._shared.http.errors import HttpApiError
from lazycloud.cli.components.errors import (
    normalize_exception,
)


def _transport_error(body: str, *, status: int, url: str) -> HttpApiError:
    exc = HttpApiError(body, status_code=status)
    exc.__cause__ = urllib.error.HTTPError(
        url,
        status,
        "Bad Gateway",
        email.message.Message(),
        io.BytesIO(),
    )
    return exc


@pytest.mark.parametrize(
    "case",
    ["html", "oversized", "structured", "bare", "auth", "timeout", "bounded", "empty"],
)
def test_normalize_exception_preserves_safe_actionable_details(case: str) -> None:
    if case == "html":
        details = normalize_exception(
            _transport_error(
                "<!DOCTYPE html><html><body>wall of html</body></html>",
                status=502,
                url="http://127.0.0.1:9999/api/v1/tasks?limit=100",
            )
        )
        assert details.message == "HTTP 502 from http://127.0.0.1:9999"
        assert details.exit_code == 1
        assert "html" not in details.message
    elif case == "oversized":
        details = normalize_exception(
            _transport_error(
                "upstream exploded " * 60,
                status=503,
                url="https://api.lazycloud.dev/api/v1/tasks",
            )
        )
        assert details.message == "HTTP 503 from https://api.lazycloud.dev"
    elif case == "structured":
        plain = normalize_exception(
            _transport_error(
                "service restarting",
                status=502,
                url="http://127.0.0.1:8000/api/v1",
            )
        )
        structured = normalize_exception(HttpApiError('{"detail":"volume busy"}', status_code=409))
        assert plain.message == "service restarting"
        assert structured.message == "volume busy"
    elif case == "bare":
        details = normalize_exception(HttpApiError("<html>edge page</html>", status_code=530))
        assert details.message == "HTTP 530"
    elif case == "auth":
        unauthorized = normalize_exception(
            _transport_error(
                "<html>login page</html>",
                status=401,
                url="http://127.0.0.1:8000/api/v1",
            )
        )
        forbidden = normalize_exception(HttpApiError("Forbidden", status_code=403))
        invalid = normalize_exception(HttpApiError('{"detail":"invalid token"}', status_code=401))
        assert (unauthorized.type, unauthorized.message) == (
            "authentication_failed",
            "unauthorized",
        )
        assert (forbidden.type, forbidden.message) == ("permission_denied", "Forbidden")
        assert forbidden.title == "Access denied"
        assert "workspace" in forbidden.hint
        assert invalid.message == "invalid token"
        assert "lazycloud login" in unauthorized.hint
    elif case == "timeout":
        details = normalize_exception(TimeoutError("request timed out"))
        assert (details.type, details.title) == ("request_timed_out", "Request timed out")
    elif case == "bounded":
        cause = "invalid authorization credential"
        message = "registry inspection failed: " + ("diagnostic " * 60) + cause
        wrapped = RuntimeError(message)
        wrapped.__cause__ = RuntimeError(message)
        details = normalize_exception(wrapped)
        assert details.type == "unexpected_error"
        assert details.message.startswith("registry inspection failed:")
        assert cause in details.message
        assert "[truncated]" in details.message
        assert len(details.message) <= 400
    else:
        wrapped = RuntimeError()
        wrapped.__cause__ = RuntimeError("specific downstream failure")
        assert normalize_exception(wrapped).message == "specific downstream failure"


def test_cli_console_prints_through_a_live_stdout_proxy(capsys: pytest.CaptureFixture[str]) -> None:
    from lazycloud._terminal.streams import OutputConsole
    from rich.file_proxy import FileProxy

    console = OutputConsole(force_terminal=False)
    original = sys.stdout
    sys.stdout = FileProxy(console, original)
    try:
        console.print("through the proxy")
    finally:
        sys.stdout = original

    assert "through the proxy" in capsys.readouterr().out

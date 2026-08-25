from __future__ import annotations

import http.client
import io
import urllib.error

import pytest
from shared.http.errors import HttpApiError
from shared.transport_retry import (
    is_transient_transport_error,
)


def _http_error(status: int = 404) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://control-plane/api/v1/tasks/task-1",
        status,
        "not found",
        http.client.HTTPMessage(),
        io.BytesIO(b""),
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ConnectionResetError("peer reset"), True),
        (ConnectionRefusedError("refused"), True),
        (TimeoutError("timed out"), True),
        (http.client.RemoteDisconnected("closed"), True),
        (urllib.error.URLError(ConnectionRefusedError("refused")), True),
        (_http_error(404), False),
        (_http_error(500), False),
        (HttpApiError("task not found", status_code=404), False),
        (ValueError("bad payload"), False),
    ],
)
def test_transient_transport_error_classification(error: Exception, expected: bool) -> None:
    assert is_transient_transport_error(error) is expected

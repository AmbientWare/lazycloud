from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
from collections.abc import Iterator

import pytest
import typer
from lazycloud.cli.components.errors import (
    normalize_exception,
)
from lazycloud.cli.components.output import (
    CliContextState,
    console,
    error_console,
    print_payload,
    set_json_output,
)
from shared.http.errors import HttpApiError
from typer.core import TyperCommand


@pytest.fixture
def human_output_mode() -> Iterator[None]:
    set_json_output(False)
    yield
    set_json_output(False)


def _context(*, json_output: bool) -> typer.Context:
    context = typer.Context(TyperCommand(name="test"))
    context.obj = CliContextState(json=json_output)
    return context


def test_output_channels_preserve_json_cleanliness_and_restore_human_state(
    capsys: pytest.CaptureFixture[str],
    human_output_mode: None,
) -> None:
    print_payload(_context(json_output=True), {"b": 1, "a": {"z": None, "y": [2, "x"]}})
    captured = capsys.readouterr()
    assert captured.out == '{"a": {"y": [2, "x"], "z": null}, "b": 1}\n'
    assert captured.err == ""

    print_payload(_context(json_output=False), "deployed handler")
    captured = capsys.readouterr()
    assert "deployed handler" in captured.out
    assert captured.err == ""

    print_payload(_context(json_output=False), {"phase": "ready", "attempts": 2})
    captured = capsys.readouterr()
    assert "Result" in captured.out
    assert "Phase" in captured.out
    assert "ready" in captured.out
    assert "{'phase'" not in captured.out

    set_json_output(True)
    console.print("Tip: run lazycloud cloud validate next.")
    print_payload(_context(json_output=True), {"phase": "ready"})
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"phase": "ready"}
    assert "Tip: run lazycloud cloud validate next." in captured.err

    set_json_output(True)
    set_json_output(False)
    console.print("human line")
    captured = capsys.readouterr()
    assert "human line" in captured.out
    assert captured.err == ""

    error_console.print("problem detail")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "problem detail" in captured.err


def test_stream_output_preserves_rich_looking_user_text(
    capsys: pytest.CaptureFixture[str],
    human_output_mode: None,
) -> None:
    from lazycloud.cli.components.output import write_stream

    write_stream("[red]literal[/red]\n")

    captured = capsys.readouterr()
    assert captured.out == "[red]literal[/red]\n"
    assert captured.err == ""


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
    from lazycloud.cli.components.output import CliConsole
    from rich.file_proxy import FileProxy

    console = CliConsole(force_terminal=False)
    original = sys.stdout
    sys.stdout = FileProxy(console, original)
    try:
        console.print("through the proxy")
    finally:
        sys.stdout = original

    assert "through the proxy" in capsys.readouterr().out

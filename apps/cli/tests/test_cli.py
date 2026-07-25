from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

import pytest
from botocore.exceptions import EndpointConnectionError
from cli.components.errors import ADMIN_ERROR_POLICY
from cli.main import build_admin_cli, start
from lazycloud.cli.components.errors import normalize_exception
from lazycloud.cli.handler_workflows import HandlerLoadError, load_handler_object
from lazycloud.cli.main import normalize_global_flags
from lazycloud.json_contracts import JsonValue, parse_json_object, parse_json_value
from psycopg import OperationalError as PsycopgOperationalError
from redis.exceptions import ConnectionError as RedisConnectionError
from shared.app_identity import CLI_NAME
from shared.http.compute import (
    PoolCapacityExtendRequest,
    PoolCapacityResponse,
    PoolJoinCommandRequest,
    PoolJoinCommandResponse,
    PoolJoinTokenRequest,
    PoolJoinTokenResponse,
    PoolMachineListResponse,
    PoolMachineResponse,
)

cli = build_admin_cli()


class _CliEndpointHeaders:
    def keys(self) -> list[str]:
        return ["content-type"]

    def __iter__(self) -> Iterator[str]:
        return iter(self.keys())

    def get_all(self, key: str) -> list[str]:
        assert key == "content-type"
        return ["application/json"]


class _CliEndpointHttpResponse:
    status = 200
    headers = _CliEndpointHeaders()

    def __enter__(self) -> _CliEndpointHttpResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = exc_type, exc, traceback

    def read(self) -> bytes:
        return b'{"ok": true}'

    def geturl(self) -> str:
        return "http://127.0.0.1:9000/endpoint/id/stub-cold-start"


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_runtime_error_output_modes_are_stable_and_traceback_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_mode: bool,
) -> None:
    module = tmp_path / "failing_handlers.py"
    module.write_text(
        'def fails():\n    raise RuntimeError(\'{"detail":"invalid token"}\')\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    args = ["run", "failing_handlers:fails", *(["--json"] if json_mode else [])]

    with pytest.raises(SystemExit) as raised:
        start(args=args, prog_name=CLI_NAME)

    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    if json_mode:
        payload = _json_object(captured.err, "CLI error output")
        assert _json_path(payload, "error", "type") == "unexpected_error"
        assert _json_path(payload, "error", "message") == "invalid token"
    else:
        assert "Unexpected error" in captured.err
        assert "invalid token" in captured.err
        assert "--debug" in captured.err


@dataclass
class _FakePoolComputeClient:
    extend_requests: list[tuple[str, PoolCapacityExtendRequest]] = field(default_factory=list)
    token_requests: list[tuple[str, PoolJoinTokenRequest]] = field(default_factory=list)
    revoked_pools: list[str] = field(default_factory=list)
    command_requests: list[tuple[str, PoolJoinCommandRequest]] = field(default_factory=list)
    machine_pools: list[str] = field(default_factory=list)

    def extend_pool_capacity(
        self,
        pool_name: str,
        request: PoolCapacityExtendRequest,
    ) -> PoolCapacityResponse:
        self.extend_requests.append((pool_name, request))
        return PoolCapacityResponse(name=pool_name, max_spend_micros=10_000_000)

    def create_pool_join_token(
        self,
        pool_name: str,
        request: PoolJoinTokenRequest,
    ) -> PoolJoinTokenResponse:
        self.token_requests.append((pool_name, request))
        return PoolJoinTokenResponse(
            token="join-token",
            expires_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        )

    def revoke_pool_join_token(self, pool_name: str) -> None:
        self.revoked_pools.append(pool_name)

    def pool_join_command(
        self,
        pool_name: str,
        request: PoolJoinCommandRequest,
    ) -> PoolJoinCommandResponse:
        self.command_requests.append((pool_name, request))
        return PoolJoinCommandResponse(
            command=f"{CLI_NAME} agent join --token join-token",
            expires_at=datetime(2026, 1, 1, 0, 30, tzinfo=UTC),
        )

    def list_pool_machines(
        self,
        pool_name: str,
        *,
        limit: int = 100,
        cursor: str = "",
    ) -> PoolMachineListResponse:
        del limit, cursor
        self.machine_pools.append(pool_name)
        return PoolMachineListResponse(
            data=[PoolMachineResponse(id="machine-1", pool_name=pool_name, status="ready")]
        )


def _json_object(raw: str, name: str) -> dict[str, JsonValue]:
    try:
        return parse_json_object(raw)
    except ValueError as exc:
        raise AssertionError(f"{name} must be a JSON object") from exc


def _json_objects(raw: str, name: str) -> list[dict[str, JsonValue]]:
    value = parse_json_value(raw)
    if not isinstance(value, list):
        raise AssertionError(f"{name} must be a JSON array")
    items: list[dict[str, JsonValue]] = []
    for item in value:
        if not isinstance(item, dict):
            raise AssertionError(f"{name} items must be JSON objects")
        items.append(item)
    return items


def _json_path(value: JsonValue, *path: str | int) -> JsonValue:
    current = value
    for segment in path:
        if isinstance(segment, str):
            if not isinstance(current, dict):
                raise AssertionError(f"expected JSON object before {segment!r}")
            current = current[segment]
        else:
            if not isinstance(current, list):
                raise AssertionError(f"expected JSON array before index {segment}")
            current = current[segment]
    return current


def _json_string(value: JsonValue, *path: str | int) -> str:
    selected = _json_path(value, *path)
    if not isinstance(selected, str):
        raise AssertionError(f"expected JSON string at {path!r}")
    return selected


def _unwrapped_output(output: str) -> str:
    return " ".join(output.replace("│", " ").split())


def test_cli_path_handler_outside_current_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    module = outside / "faraway.py"
    module.write_text("def handler():\n    return 1\n", encoding="utf-8")
    inside = tmp_path / "project"
    inside.mkdir()
    monkeypatch.chdir(inside)

    with pytest.raises(HandlerLoadError, match="outside the current directory"):
        load_handler_object(f"{module}:handler")


def test_cli_start_debug_reraises_runtime_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = tmp_path / "debug_failing_handlers.py"
    module.write_text(
        """
def fails():
    raise RuntimeError('{"detail":"invalid token"}')
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="invalid token"):
        start(args=["run", "debug_failing_handlers:fails", "--debug"], prog_name=CLI_NAME)


def test_cli_global_flag_normalization_preserves_command_separator() -> None:
    assert normalize_global_flags(["run", "module:handler", "--json"]) == [
        "--json",
        "run",
        "module:handler",
    ]
    assert normalize_global_flags(["run", "module:handler", "--", "--json"]) == [
        "run",
        "module:handler",
        "--",
        "--json",
    ]


def test_cli_error_normalization_masks_tokens() -> None:
    details = normalize_exception(
        RuntimeError("request failed for rt_abcdefghijklmnopqrstuvwxyz"),
        policy=ADMIN_ERROR_POLICY,
    )

    assert details.message == "request failed for rt_a...wxyz"
    assert "abcdefghijklmnopqrstuvwxyz" not in details.message


@pytest.mark.parametrize(
    ("owner", "expected_hints", "forbidden_hints"),
    [
        ("database", ("LAZYCLOUD_DATABASE_URL",), ("control plane", "profile endpoint")),
        ("redis", ("LAZYCLOUD_REDIS_URL",), ("control plane",)),
        (
            "object-store",
            (
                "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL",
                "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID",
                "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY",
            ),
            (),
        ),
        ("control-plane", ("control plane", "profile"), ()),
    ],
)
def test_cli_connection_failures_identify_their_configuration_owner(
    owner: str,
    expected_hints: tuple[str, ...],
    forbidden_hints: tuple[str, ...],
) -> None:
    if owner == "database":
        error = RuntimeError("database check failed")
        error.__cause__ = PsycopgOperationalError(
            'connection failed: connection to server at "127.0.0.1", port 5999 failed: '
            "Connection refused"
        )
    elif owner == "redis":
        error = RedisConnectionError("Error 61 connecting to 127.0.0.1:6399. Connection refused.")
    elif owner == "object-store":
        error = EndpointConnectionError(endpoint_url="http://127.0.0.1:9002")
        error.__cause__ = ConnectionRefusedError("Connection refused")
    else:
        error = urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    details = normalize_exception(error, policy=ADMIN_ERROR_POLICY)

    assert details.type == "control_plane_unavailable"
    assert all(hint in details.hint for hint in expected_hints)
    assert all(hint not in details.hint for hint in forbidden_hints)

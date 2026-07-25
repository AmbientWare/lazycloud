from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from lazycloud.clients.function.control import FunctionControlClient
from lazycloud.clients.shell.control import ShellControlClient
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.function_payloads import FunctionJsonInvocation
from shared.http.errors import HttpResponseDecodeError, http_api_error_from_body
from tests.contracts.http_contract_cases import load_contract_corpus

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


@dataclass
class _FakeShellChannel:
    response: dict[str, JsonValue]

    def get(self, path: str) -> JsonValue:
        raise AssertionError(path)

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        assert path == "/api/v1/shells/existing-container"
        assert payload == {"container_id": "container-contract"}
        return self.response


@dataclass
class _FakeFunctionChannel:
    response: dict[str, JsonValue]

    def post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> JsonValue:
        raise AssertionError((path, payload))

    def stream_post(
        self,
        path: str,
        payload: dict[str, JsonValue] | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        assert path == "/api/v1/functions/invoke/stream"
        assert payload is not None
        assert payload["stub_id"] == "stub-contract"
        yield self.response


def test_sdk_decoders_consume_python_owned_contract_cases() -> None:
    corpus = load_contract_corpus()

    for case in corpus["cases"]:
        if case["contract"] == "create_shell_in_existing_container_response":
            _assert_shell_case(case["input"], case["accepted"], case["normalized"])
        elif case["contract"] == "function_invoke_response":
            _assert_function_case(case["input"], case["accepted"], case["normalized"])
        else:
            _assert_error_case(case["input"], case["accepted"], case["normalized"])


def _assert_shell_case(
    payload: dict[str, JsonValue],
    accepted: bool,
    normalized: dict[str, JsonValue] | None,
) -> None:
    client = ShellControlClient(channel=_FakeShellChannel(response=payload))
    if not accepted:
        with pytest.raises(ValidationError):
            client.create_existing("container-contract")
        return
    response = client.create_existing("container-contract")
    assert _JSON_OBJECT_ADAPTER.validate_python(response.model_dump(mode="json")) == normalized


def _assert_function_case(
    payload: dict[str, JsonValue],
    accepted: bool,
    normalized: dict[str, JsonValue] | None,
) -> None:
    client = FunctionControlClient(channel=_FakeFunctionChannel(response=payload))
    if not accepted:
        with pytest.raises(HttpResponseDecodeError):
            next(client.invoke("stub-contract", FunctionJsonInvocation()))
        return
    response = next(client.invoke("stub-contract", FunctionJsonInvocation()))
    assert _JSON_OBJECT_ADAPTER.validate_python(response.model_dump(mode="json")) == normalized


def _assert_error_case(
    payload: dict[str, JsonValue],
    accepted: bool,
    normalized: dict[str, JsonValue] | None,
) -> None:
    error = http_api_error_from_body(409, _JSON_OBJECT_ADAPTER.dump_json(payload).decode())
    if accepted:
        assert error.error is not None
        decoded = _JSON_OBJECT_ADAPTER.validate_python(error.error.model_dump(mode="json"))
        assert decoded == normalized
    else:
        assert error.error is None

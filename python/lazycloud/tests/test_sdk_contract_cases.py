from __future__ import annotations

from dataclasses import dataclass

import pytest
from lazycloud.clients.shell.control import ShellControlClient
from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.http.errors import http_api_error_from_body

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
        _ = path, payload
        return self.response


def test_sdk_decoders_consume_python_owned_contract_cases() -> None:
    corpus = load_contract_corpus()

    for case in corpus["cases"]:
        if case["contract"] == "create_shell_in_existing_container_response":
            _assert_shell_case(case["input"], case["accepted"], case["normalized"])
        elif case["contract"] == "function_invoke_response":
            # The SDK no longer calls the old invoke endpoint; web still reads these cases.
            continue
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

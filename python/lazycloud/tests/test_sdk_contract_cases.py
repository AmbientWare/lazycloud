from __future__ import annotations

from pydantic import JsonValue, TypeAdapter
from shared.http.errors import http_api_error_from_body

from tests.contracts.http_contract_cases import load_contract_corpus

_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_sdk_decoders_consume_python_owned_contract_cases() -> None:
    corpus = load_contract_corpus()

    for case in corpus["cases"]:
        if case["contract"] != "error_response":
            # The SDK no longer calls the old invoke and shell endpoints.
            continue
        _assert_error_case(case["input"], case["accepted"], case["normalized"])


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

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import JsonValue, TypeAdapter, ValidationError
from shared.function_payloads import FunctionPayloadEncoding
from shared.http.base import HttpModel
from shared.http.errors import ErrorResponse
from shared.http.functions import FunctionInvokeResponse
from shared.http.shells import CreateShellInExistingContainerResponse

ContractName = Literal[
    "create_shell_in_existing_container_response",
    "error_response",
    "function_invoke_response",
]

CORPUS_PATH = Path(__file__).with_name("http_contract_cases.json")
FORMAT_VERSION = 1


class ContractCasePayload(TypedDict):
    contract: ContractName
    name: str
    accepted: bool
    input: dict[str, JsonValue]
    normalized: dict[str, JsonValue] | None


class ContractCorpusPayload(TypedDict):
    format_version: Literal[1]
    cases: list[ContractCasePayload]


@dataclass(frozen=True, slots=True)
class _InputCase:
    contract: ContractName
    name: str
    accepted: bool
    input: dict[str, JsonValue]


class _Arguments(argparse.Namespace):
    check: bool = False
    output: Path = CORPUS_PATH


_MODEL_BY_CONTRACT: dict[ContractName, type[HttpModel]] = {
    "create_shell_in_existing_container_response": CreateShellInExistingContainerResponse,
    "error_response": ErrorResponse,
    "function_invoke_response": FunctionInvokeResponse,
}
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
_CORPUS_ADAPTER = TypeAdapter(ContractCorpusPayload)


def build_contract_corpus() -> ContractCorpusPayload:
    cases: list[ContractCasePayload] = []
    for input_case in _input_cases():
        model = _MODEL_BY_CONTRACT[input_case.contract]
        try:
            parsed = model.model_validate(input_case.input)
        except ValidationError as exc:
            if input_case.accepted:
                msg = f"accepted case {input_case.contract}/{input_case.name} is invalid"
                raise AssertionError(msg) from exc
            normalized = None
        else:
            if not input_case.accepted:
                msg = f"rejected case {input_case.contract}/{input_case.name} is valid"
                raise AssertionError(msg)
            normalized = _JSON_OBJECT_ADAPTER.validate_python(parsed.model_dump(mode="json"))
        cases.append(
            {
                "contract": input_case.contract,
                "name": input_case.name,
                "accepted": input_case.accepted,
                "input": input_case.input,
                "normalized": normalized,
            }
        )
    return {"format_version": FORMAT_VERSION, "cases": cases}


def render_contract_corpus() -> str:
    return json.dumps(build_contract_corpus(), indent=2, sort_keys=True) + "\n"


def load_contract_corpus() -> ContractCorpusPayload:
    return _CORPUS_ADAPTER.validate_json(CORPUS_PATH.read_bytes())


def _input_cases() -> list[_InputCase]:
    cases = [
        _InputCase(
            contract="create_shell_in_existing_container_response",
            name="valid_ticketed_session",
            accepted=True,
            input={
                "username": "runner",
                "password": "session-password",
                "stub_id": "stub-shell",
                "websocket_ticket": "wst_single_use",
            },
        ),
        _InputCase(
            contract="create_shell_in_existing_container_response",
            name="missing_ticket",
            accepted=False,
            input={
                "username": "runner",
                "password": "session-password",
                "stub_id": "stub-shell",
            },
        ),
        _InputCase(
            contract="create_shell_in_existing_container_response",
            name="empty_ticket",
            accepted=False,
            input={
                "username": "runner",
                "password": "session-password",
                "stub_id": "stub-shell",
                "websocket_ticket": "",
            },
        ),
        _InputCase(
            contract="create_shell_in_existing_container_response",
            name="null_ticket",
            accepted=False,
            input={
                "username": "runner",
                "password": "session-password",
                "stub_id": "stub-shell",
                "websocket_ticket": None,
            },
        ),
        _InputCase(
            contract="create_shell_in_existing_container_response",
            name="unknown_field",
            accepted=False,
            input={
                "username": "runner",
                "password": "session-password",
                "stub_id": "stub-shell",
                "websocket_ticket": "wst_single_use",
                "token": "not-a-contract-field",
            },
        ),
        _InputCase(
            contract="error_response",
            name="valid_detail",
            accepted=True,
            input={"detail": "workspace access denied"},
        ),
        _InputCase(
            contract="error_response",
            name="missing_detail",
            accepted=False,
            input={},
        ),
        _InputCase(
            contract="error_response",
            name="null_detail",
            accepted=False,
            input={"detail": None},
        ),
        _InputCase(
            contract="error_response",
            name="unknown_field",
            accepted=False,
            input={"detail": "workspace access denied", "message": "duplicate owner"},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="omitted_defaults",
            accepted=True,
            input={},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="nullable_result",
            accepted=True,
            input={"task_id": "task-null", "result": None},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="null_non_nullable_task_id",
            accepted=False,
            input={"task_id": None},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="missing_result_discriminator",
            accepted=False,
            input={"task_id": "task-missing-tag", "result": {"value": {"ok": True}}},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="invalid_result_encoding",
            accepted=False,
            input={"task_id": "task-invalid-tag", "result": {"encoding": "yaml"}},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="unknown_top_level_field",
            accepted=False,
            input={"task_id": "task-extra", "operation": "invoke"},
        ),
        _InputCase(
            contract="function_invoke_response",
            name="unknown_nested_field",
            accepted=False,
            input={
                "task_id": "task-extra-result",
                "result": {"encoding": "json", "value": None, "schema": "hidden"},
            },
        ),
    ]
    cases.extend(_function_result_encoding_cases())
    return cases


def _function_result_encoding_cases() -> list[_InputCase]:
    result_by_encoding: dict[FunctionPayloadEncoding, dict[str, JsonValue]] = {
        FunctionPayloadEncoding.Json: {
            "version": 1,
            "encoding": FunctionPayloadEncoding.Json.value,
            "value": {"answer": 42, "ready": True},
        },
        FunctionPayloadEncoding.Cloudpickle: {
            "version": 1,
            "encoding": FunctionPayloadEncoding.Cloudpickle.value,
            "value_base64": "gASVCgAAAAAAAAB9lIwCb2uUiHMu",
            "size_bytes": 21,
            "sha256": "2bee21166d3fcc8ca23b9e3a80c18dc6ab281d3e49b920d18bcf2771672a0a69",
        },
    }
    if set(result_by_encoding) != set(FunctionPayloadEncoding):
        raise AssertionError("function result encoding cases must be exhaustive")
    return [
        _InputCase(
            contract="function_invoke_response",
            name=f"result_encoding_{encoding.value}",
            accepted=True,
            input={
                "task_id": f"task-{encoding.value}",
                "output": "complete\n",
                "done": True,
                "exit_code": 0,
                "result": result,
            },
        )
        for encoding, result in result_by_encoding.items()
    ]


def _main() -> None:
    parser = argparse.ArgumentParser(description="Export cross-runtime HTTP contract cases.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=CORPUS_PATH)
    args = parser.parse_args(namespace=_Arguments())
    rendered = render_contract_corpus()
    if args.check:
        if not args.output.is_file() or args.output.read_text() != rendered:
            raise SystemExit(f"contract case corpus is stale: {args.output}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)


if __name__ == "__main__":
    _main()

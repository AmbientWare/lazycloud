from __future__ import annotations

from pydantic import JsonValue, TypeAdapter
from shared.http.base import HttpModel
from shared.http.errors import ErrorResponse
from shared.http.functions import FunctionInvokeResponse
from shared.http.shells import CreateShellInExistingContainerResponse
from tests.contracts.http_contract_cases import (
    ContractName,
)

_MODEL_BY_CONTRACT: dict[ContractName, type[HttpModel]] = {
    "create_shell_in_existing_container_response": CreateShellInExistingContainerResponse,
    "error_response": ErrorResponse,
    "function_invoke_response": FunctionInvokeResponse,
}
_JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])

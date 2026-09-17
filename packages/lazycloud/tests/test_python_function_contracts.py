from __future__ import annotations

import pytest
from lazycloud.client_contracts import ClientContractError
from lazycloud.function_results import FunctionResultDecodeError, decode_function_result
from shared.function_payloads import FunctionCloudpickleResult

from lazycloud import App


class PythonValue:
    def __init__(self, number: int = 7) -> None:
        self.number = number


def test_python_function_contract_preserves_opaque_types_and_defaults() -> None:
    default = PythonValue()

    @App("python_values").function()
    def echo(value: PythonValue = default) -> PythonValue:
        return value

    contract = echo.spec().client_contract
    assert contract is not None
    parameter = contract.operation.parameters[0]
    assert parameter.json_schema is None
    assert parameter.python_type.endswith("PythonValue")
    assert not parameter.required
    assert parameter.python_default
    assert parameter.default is None
    assert parameter.default_repr == ""
    assert contract.operation.return_schema is None
    assert contract.operation.return_python_type.endswith("PythonValue")
    assert contract.json_export_errors()
    assert echo.local() is default


def test_constructor_annotation_is_rejected() -> None:
    def invalid() -> int:
        return 0

    invalid.__annotations__["return"] = len
    with pytest.raises(ClientContractError, match="use a type"):
        App("invalid_annotation").function()(invalid).spec()


def test_missing_result_dependency_names_the_missing_module() -> None:
    payload = FunctionCloudpickleResult.from_bytes(b"clazycloud_missing_user_dependency\nValue\n.")
    with pytest.raises(FunctionResultDecodeError, match=r"missing dependency.*lazycloud_missing"):
        decode_function_result(payload)

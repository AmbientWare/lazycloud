from __future__ import annotations

from typing import NewType, TypeVar

import pytest
from lazycloud.client_contracts import ClientContractError, build_client_contract
from lazycloud.function_results import FunctionResultDecodeError, decode_function_result
from shared.deployments import DeploymentKind
from shared.function_payloads import FunctionCloudpickleResult
from typing_extensions import TypeAliasType

from lazycloud import App


class PythonValue:
    def __init__(self, number: int = 7) -> None:
        self.number = number


PythonAlias = TypeAliasType("PythonAlias", PythonValue)
PythonNewType = NewType("PythonNewType", PythonValue)
PythonTypeVar = TypeVar("PythonTypeVar", bound=PythonValue)


def test_python_contract_accepts_opaque_aliases_and_type_variables() -> None:
    def echo(alias: PythonAlias, wrapped: PythonNewType, generic: PythonTypeVar) -> PythonTypeVar:
        return generic

    contract = build_client_contract(echo, kind=DeploymentKind.Function)

    assert contract is not None
    assert all(parameter.python_type for parameter in contract.operation.parameters)
    assert all(parameter.json_schema is None for parameter in contract.operation.parameters)
    assert contract.operation.return_python_type
    assert contract.operation.return_schema is None


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

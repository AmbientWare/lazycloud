from __future__ import annotations

from typing import TypeAlias

from pydantic import Field

from shared.contracts import ContractModel
from shared.enums import StringEnum

SIGNAL_KEY_PREFIX = "signal"
DEFAULT_SIGNAL_SET_TTL_SECONDS = 600

SignalValue: TypeAlias = str | bytes | int | float | bool | None


class SignalOperation(StringEnum):
    Set = "set"
    Clear = "clear"
    Read = "read"
    Monitor = "monitor"


class SignalStatus(StringEnum):
    Accepted = "accepted"
    InvalidName = "invalid-name"
    Missing = "missing"
    Found = "found"
    RepositoryError = "repository-error"


class SignalSetRequest(ContractModel):
    workspace_name: str
    name: str
    ttl_seconds: int = DEFAULT_SIGNAL_SET_TTL_SECONDS


class SignalClearRequest(ContractModel):
    workspace_name: str
    name: str


class SignalMonitorRequest(ContractModel):
    workspace_name: str
    name: str


class SignalSetResponse(ContractModel):
    pass


class SignalClearResponse(ContractModel):
    pass


class SignalMonitorResponse(ContractModel):
    set: bool = False


class SignalSetPlan(ContractModel):
    operation: SignalOperation = SignalOperation.Set
    status: SignalStatus = SignalStatus.Accepted
    signal_key: str = ""
    ttl_seconds: int = DEFAULT_SIGNAL_SET_TTL_SECONDS
    value: str = "1"
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SignalStatus.Accepted


class SignalClearPlan(ContractModel):
    operation: SignalOperation = SignalOperation.Clear
    status: SignalStatus = SignalStatus.Accepted
    signal_key: str = ""
    deleted: bool = False
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SignalStatus.Accepted


class SignalReadPlan(ContractModel):
    operation: SignalOperation = SignalOperation.Read
    status: SignalStatus = SignalStatus.Missing
    signal_key: str = ""
    set: bool = False
    raw_value: str = ""
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {SignalStatus.Found, SignalStatus.Missing}


class SignalMonitorSnapshot(ContractModel):
    operation: SignalOperation = SignalOperation.Monitor
    status: SignalStatus = SignalStatus.Missing
    signal_key: str = ""
    ok: bool = True
    set: bool = False
    should_call_handler: bool = False
    should_clear_after_handler: bool = False
    clear_after_seconds: int = 0
    error_message: str = ""
    response: SignalMonitorResponse = Field(default_factory=SignalMonitorResponse)


def signal_name(workspace_name: str, name: str) -> str:
    return f"{SIGNAL_KEY_PREFIX}:{workspace_name}:{name}"


def normalize_signal_ttl(ttl_seconds: int | None) -> int:
    if ttl_seconds is None or ttl_seconds <= 0:
        return DEFAULT_SIGNAL_SET_TTL_SECONDS
    return ttl_seconds


def plan_signal_set(request: SignalSetRequest) -> SignalSetPlan:
    error = _identity_error(request.workspace_name, request.name)
    signal_key = signal_name(request.workspace_name, request.name)
    if error:
        return SignalSetPlan(
            status=SignalStatus.InvalidName,
            signal_key=signal_key,
            error_message=error,
        )
    return SignalSetPlan(
        signal_key=signal_key,
        ttl_seconds=normalize_signal_ttl(request.ttl_seconds),
    )


def plan_signal_clear(workspace_name: str, name: str, *, deleted: bool = False) -> SignalClearPlan:
    error = _identity_error(workspace_name, name)
    signal_key = signal_name(workspace_name, name)
    if error:
        return SignalClearPlan(
            status=SignalStatus.InvalidName,
            signal_key=signal_key,
            error_message=error,
        )
    return SignalClearPlan(signal_key=signal_key, deleted=deleted)


def plan_signal_read(workspace_name: str, name: str, value: SignalValue) -> SignalReadPlan:
    error = _identity_error(workspace_name, name)
    signal_key = signal_name(workspace_name, name)
    if error:
        return SignalReadPlan(
            status=SignalStatus.InvalidName,
            signal_key=signal_key,
            error_message=error,
        )
    if value is None:
        return SignalReadPlan(signal_key=signal_key)
    raw = _value_text(value)
    return SignalReadPlan(
        status=SignalStatus.Found,
        signal_key=signal_key,
        set=signal_value_is_set(value),
        raw_value=raw,
    )


def plan_signal_monitor_snapshot(
    workspace_name: str,
    name: str,
    value: SignalValue,
    *,
    has_handler: bool = False,
    clear_after_interval_seconds: int | None = None,
) -> SignalMonitorSnapshot:
    read = plan_signal_read(workspace_name, name, value)
    ok = read.ok
    should_call = ok and read.set and has_handler
    clear_after = normalize_clear_after_interval(clear_after_interval_seconds)
    should_clear = should_call and clear_after > 0
    return SignalMonitorSnapshot(
        status=read.status,
        signal_key=read.signal_key,
        ok=ok,
        set=read.set,
        should_call_handler=should_call,
        should_clear_after_handler=should_clear,
        clear_after_seconds=clear_after,
        error_message=read.error_message,
        response=SignalMonitorResponse(set=read.set),
    )


def normalize_clear_after_interval(value: int | None) -> int:
    if value is None or value <= 0:
        return 0
    return value


def signal_value_is_set(value: SignalValue) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    return _value_text(value).strip() == "1"


def signal_response_from_set_plan(_plan: SignalSetPlan) -> SignalSetResponse:
    return SignalSetResponse()


def signal_response_from_clear_plan(_plan: SignalClearPlan) -> SignalClearResponse:
    return SignalClearResponse()


def _identity_error(workspace_name: str, name: str) -> str:
    if not workspace_name.strip():
        return "signal workspace name is required"
    if not name.strip():
        return "signal name is required"
    return ""


def _value_text(value: SignalValue) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return str(value)

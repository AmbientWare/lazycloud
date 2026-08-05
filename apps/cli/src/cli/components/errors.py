from __future__ import annotations

import re

from lazycloud.cli.components.errors import (
    ClientErrorDetails,
    CliErrorPolicy,
    exception_chain,
)
from shared.app_identity import ADMIN_CLI_NAME, ENV_PREFIX
from shared.deployment_settings import MissingDeploymentSettingError
from shared.errors import (
    ConflictError,
    DomainError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)


def _domain_error_details(
    exc: BaseException,
    message: str,
) -> ClientErrorDetails | None:
    if not isinstance(exc, DomainError):
        return None
    error_type, title = _DOMAIN_ERROR_DETAILS.get(
        type(exc),
        ("request_failed", "Request failed"),
    )
    return ClientErrorDetails(type=error_type, title=title, message=message)


def _missing_setting_details(
    exc: BaseException,
    message: str,
) -> ClientErrorDetails | None:
    for item in exception_chain(exc):
        if isinstance(item, MissingDeploymentSettingError):
            return ClientErrorDetails(
                type="configuration_missing",
                title="Configuration missing",
                message=message,
                hint=(
                    f"Export `{item.variable}` or set it in the `.env` the stack "
                    f"reads; `{ADMIN_CLI_NAME}` connects to backends directly and "
                    "does not inherit a container's environment."
                ),
            )
    return None


def _operation_error_details(
    exc: BaseException,
    message: str,
) -> ClientErrorDetails | None:
    if not exc.__class__.__name__.endswith("OperationError"):
        return None
    name = exc.__class__.__name__.removesuffix("OperationError")
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip()
    return ClientErrorDetails(
        type="operation_failed",
        title=f"{words or 'Operation'} failed",
        message=message,
    )


def _connection_hint(exc: BaseException) -> str:
    """Name the connection knob for the backend that actually failed."""
    modules = [type(item).__module__ for item in exception_chain(exc)]
    if any(name.startswith(("psycopg", "sqlalchemy")) for name in modules):
        return (
            f"Check that PostgreSQL is running and that `{ENV_PREFIX}_DATABASE_URL` "
            "points at it; the admin CLI connects to the database directly."
        )
    if any(name.startswith("redis") for name in modules):
        return (
            f"Check that Redis is running and that `{ENV_PREFIX}_REDIS_URL` "
            "points at it; the admin CLI connects to Redis directly."
        )
    if any(name.startswith(("botocore", "boto3")) for name in modules):
        return (
            "Check that the object store is running and that "
            f"`{ENV_PREFIX}_OBJECT_STORE_ENDPOINT_URL`, "
            f"`{ENV_PREFIX}_OBJECT_STORE_ACCESS_KEY_ID`, and "
            f"`{ENV_PREFIX}_OBJECT_STORE_SECRET_ACCESS_KEY` match it."
        )
    if any(name.startswith("urllib") for name in modules):
        return (
            "Check that the control plane is running and that the active profile "
            "endpoint is correct."
        )
    return (
        "Check that the target service is running: the active profile endpoint for "
        f"control-plane commands, or `{ENV_PREFIX}_DATABASE_URL`, "
        f"`{ENV_PREFIX}_REDIS_URL`, and `{ENV_PREFIX}_OBJECT_STORE_ENDPOINT_URL` "
        "for direct backend commands."
    )


_DOMAIN_ERROR_DETAILS: dict[type[DomainError], tuple[str, str]] = {
    NotFoundError: ("not_found", "Not found"),
    InvalidInputError: ("invalid_input", "Invalid input"),
    ConflictError: ("conflict", "Conflict"),
    UpstreamUnavailableError: ("upstream_unavailable", "Service unavailable"),
}

ADMIN_ERROR_POLICY = CliErrorPolicy(
    auth_hint=f"Run `{ADMIN_CLI_NAME} login` to refresh credentials.",
    connection_hint=_connection_hint,
    timeout_hint="Retry the command or check the service logs if the operation keeps timing out.",
    debug_hint="Run the command again with `--debug` to see the full traceback.",
    classifiers=(_missing_setting_details, _domain_error_details, _operation_error_details),
)


__all__ = ["ADMIN_ERROR_POLICY"]

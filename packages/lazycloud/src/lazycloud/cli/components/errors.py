from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

import typer
from pydantic import JsonValue
from rich.console import Console
from rich.text import Text
from shared.app_identity import ENV_PREFIX
from shared.http.errors import HttpApiError
from typer import _click as click

from lazycloud.cli.components import theme
from lazycloud.cli.components.cards import card
from lazycloud.cli.components.output import error_console, print_json_line
from lazycloud.json_contracts import parse_json_value

_TOKEN_PATTERN = re.compile(r"\brt_[A-Za-z0-9_-]{8,}\b")
_BEARER_PATTERN = re.compile(r"(Bearer\s+)([A-Za-z0-9._~+/=-]{12,})", re.IGNORECASE)
_MAX_BODY_MESSAGE_CHARS = 400
_TRUNCATED_MESSAGE_MARKER = " ... [truncated] ... "
CLIENT_CLI_NAME = "lazycloud"


@dataclass(frozen=True, slots=True)
class ClientErrorDetails:
    type: str
    title: str
    message: str
    hint: str = ""
    exit_code: int = 1

    def model_dump(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "message": self.message,
        }
        if self.hint:
            payload["hint"] = self.hint
        return {"error": payload}


class ClientError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        type: str = "command_failed",
        title: str = "Command failed",
        hint: str = "",
        exit_code: int = 1,
    ) -> None:
        self.details = ClientErrorDetails(
            type=type,
            title=title,
            message=message,
            hint=hint,
            exit_code=exit_code,
        )
        super().__init__(message)


ExceptionClassifier = Callable[[BaseException, str], ClientErrorDetails | None]
ConnectionHintResolver = Callable[[BaseException], str]


@dataclass(frozen=True, slots=True)
class CliErrorPolicy:
    auth_hint: str
    connection_hint: ConnectionHintResolver
    timeout_hint: str
    debug_hint: str
    classifiers: tuple[ExceptionClassifier, ...] = ()


def debug_errors_enabled(args: list[str] | None = None) -> bool:
    if _truthy(os.getenv(f"{ENV_PREFIX}_DEBUG", "")):
        return True
    return _root_flag_enabled(args, "--debug")


def json_errors_enabled(args: list[str] | None = None) -> bool:
    return _root_flag_enabled(args, "--json")


def normalize_exception(
    exc: BaseException,
    *,
    policy: CliErrorPolicy | None = None,
) -> ClientErrorDetails:
    if isinstance(exc, ClientError):
        return exc.details

    selected_policy = policy or CLIENT_ERROR_POLICY
    messages = [_message_from_exception(item) for item in exception_chain(exc)]
    combined = " ".join(item.lower() for item in messages if item)
    message = _first_message(messages) or _class_title(exc)

    if _is_forbidden_error(exc):
        return ClientErrorDetails(
            type="permission_denied",
            title="Access denied",
            message=message,
            hint="Check the selected workspace or ask an administrator for access.",
        )

    if _is_auth_error(exc):
        return ClientErrorDetails(
            type="authentication_failed",
            title="Authentication failed",
            message=_clean_auth_message(message),
            hint=selected_policy.auth_hint,
        )

    if _is_timeout_error(exc, combined):
        return ClientErrorDetails(
            type="request_timed_out",
            title="Request timed out",
            message=message,
            hint=selected_policy.timeout_hint,
        )

    if _is_connection_error(exc, combined):
        return ClientErrorDetails(
            type="control_plane_unavailable",
            title="Control plane unavailable",
            message=message,
            hint=selected_policy.connection_hint(exc),
        )

    coded = _details_from_error_code(exc, message)
    if coded is not None:
        return coded

    for classifier in selected_policy.classifiers:
        details = classifier(exc, message)
        if details is not None:
            return details

    return ClientErrorDetails(
        type="unexpected_error",
        title="Unexpected error",
        message=message,
        hint=selected_policy.debug_hint,
    )


_CODE_TITLES: dict[str, str] = {
    "not_found": "Not found",
    "conflict": "Conflict",
    "expired_cursor": "Cursor expired",
    "invalid_input": "Invalid input",
    "upstream_unavailable": "Upstream unavailable",
}


def _details_from_error_code(
    exc: BaseException,
    message: str,
) -> ClientErrorDetails | None:
    """Classify from the code the server sent rather than its prose."""
    for item in exception_chain(exc):
        if not isinstance(item, HttpApiError) or not item.code:
            continue
        return ClientErrorDetails(
            type=item.code,
            title=_CODE_TITLES.get(item.code, _titleize(item.code)),
            message=message,
        )
    return None


def _titleize(code: str) -> str:
    return code.replace("_", " ").capitalize()


def render_exception(
    exc: BaseException,
    *,
    json_output: bool,
    console: Console = error_console,
    policy: CliErrorPolicy | None = None,
) -> int:
    details = normalize_exception(exc, policy=policy)
    if json_output:
        print_json_line(details.model_dump(), file=console.file)
    else:
        render_error(details, console=console)
    return details.exit_code


def render_error(details: ClientErrorDetails, *, console: Console = error_console) -> None:
    body = Text()
    body.append(mask_secrets(details.message), style=theme.EMPHASIS)
    if details.hint:
        body.append("\n\n")
        body.append("Next step  ", style=theme.MUTED)
        body.append(mask_secrets(details.hint))
    console.print(card(details.title, body, tone="error"))


def mask_secrets(value: str) -> str:
    masked = _TOKEN_PATTERN.sub(_mask_token_match, value)
    return _BEARER_PATTERN.sub(
        lambda match: f"{match.group(1)}{_mask_token(match.group(2))}",
        masked,
    )


def _message_from_exception(exc: BaseException) -> str:
    if isinstance(exc, HttpApiError):
        return _http_api_error_message(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    raw = str(exc).strip()
    if not raw:
        return ""
    message = _message_from_payload(raw)
    if _is_html_body(message):
        return ""
    return _bounded_message(mask_secrets(message))


def _http_api_error_message(exc: HttpApiError) -> str:
    """Concise message for a non-success HTTP response.

    Proxies and misconfigured endpoints answer with HTML pages or other large
    non-JSON bodies; collapsing those to the status and origin keeps the error
    readable and self-diagnosing.
    """
    raw = str(exc).strip()
    if raw:
        extracted = _extracted_json_message(raw)
        if extracted:
            return mask_secrets(extracted)
        if not _is_unreadable_body(raw):
            return mask_secrets(raw)
    origin = _request_origin(exc)
    if origin:
        return f"HTTP {exc.status_code} from {origin}"
    return f"HTTP {exc.status_code}"


def _is_unreadable_body(body: str) -> bool:
    return _is_html_body(body) or len(body.lstrip()) > _MAX_BODY_MESSAGE_CHARS


def _is_html_body(body: str) -> bool:
    return body.lstrip().startswith("<")


def _bounded_message(message: str) -> str:
    if len(message) <= _MAX_BODY_MESSAGE_CHARS:
        return message
    remaining = _MAX_BODY_MESSAGE_CHARS - len(_TRUNCATED_MESSAGE_MARKER)
    head_size = (remaining * 3) // 5
    tail_size = remaining - head_size
    return message[:head_size].rstrip() + _TRUNCATED_MESSAGE_MARKER + message[-tail_size:].lstrip()


def _request_origin(exc: BaseException) -> str:
    for item in exception_chain(exc):
        if isinstance(item, urllib.error.HTTPError):
            parts = urllib.parse.urlsplit(item.url or "")
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
    return ""


def _message_from_payload(raw: str) -> str:
    text = raw.strip()
    return _extracted_json_message(text) or text


def _extracted_json_message(text: str) -> str:
    if not text.startswith(("{", "[")):
        return ""
    try:
        payload = parse_json_value(text)
    except ValueError:
        return ""
    return _payload_message(payload)


def _payload_message(payload: JsonValue) -> str:
    if isinstance(payload, dict):
        for key in ("detail", "message", "error", "err_msg", "error_msg"):
            value = payload.get(key)
            if value:
                return _payload_message(value)
        return json.dumps(payload, sort_keys=True)
    if isinstance(payload, list):
        parts = [_payload_message(item) for item in payload]
        return "; ".join(part for part in parts if part)
    return str(payload)


def exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _first_message(messages: list[str]) -> str:
    for message in messages:
        if message:
            return message
    return ""


def _class_title(exc: BaseException) -> str:
    return type(exc).__name__


def _truthy(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _root_flag_enabled(args: list[str] | None, flag: str) -> bool:
    if not args:
        return False
    for arg in args:
        if arg == "--":
            return False
        if arg == flag:
            return True
    return False


def _is_auth_error(exc: BaseException) -> bool:
    for item in exception_chain(exc):
        if isinstance(item, PermissionError):
            return True
        if isinstance(item, HttpApiError) and item.status_code == 401:
            return True
        if isinstance(item, urllib.error.HTTPError) and item.code == 401:
            return True
    return False


def _is_forbidden_error(exc: BaseException) -> bool:
    return any(
        (isinstance(item, HttpApiError) and item.status_code == 403)
        or (isinstance(item, urllib.error.HTTPError) and item.code == 403)
        for item in exception_chain(exc)
    )


def _is_connection_error(exc: BaseException, message: str) -> bool:
    return isinstance(exc, ConnectionError | urllib.error.URLError) or any(
        item in message for item in ("connection refused", "name or service not known")
    )


def _is_timeout_error(exc: BaseException, message: str) -> bool:
    return isinstance(exc, TimeoutError) or "timed out" in message


def _client_operation_classifier(
    exc: BaseException,
    message: str,
) -> ClientErrorDetails | None:
    if isinstance(exc, typer.Abort | KeyboardInterrupt):
        return ClientErrorDetails(
            type="cancelled",
            title="Cancelled",
            message="The command was cancelled.",
            exit_code=130,
        )
    if isinstance(exc, click.ClickException):
        return ClientErrorDetails(
            type="invalid_usage",
            title="Invalid command",
            message=exc.format_message(),
            hint="Run the command with --help to see the available arguments.",
            exit_code=exc.exit_code,
        )
    if isinstance(exc, ValueError):
        return ClientErrorDetails(
            type="operation_failed",
            title="Invalid command input",
            message=message,
        )
    return None


def _clean_auth_message(message: str) -> str:
    lower = message.lower()
    if "invalid token" in lower:
        return "invalid token"
    if "403" in lower or "forbidden" in lower:
        return "forbidden"
    if "401" in lower or "unauthorized" in lower:
        return "unauthorized"
    return message or "authentication failed"


def _mask_token_match(match: re.Match[str]) -> str:
    return _mask_token(match.group(0))


def _mask_token(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _client_connection_hint(exc: BaseException) -> str:
    del exc
    return (
        "Check that the control plane is running and that the active profile endpoint is correct."
    )


CLIENT_ERROR_POLICY = CliErrorPolicy(
    auth_hint=f"Run `{CLIENT_CLI_NAME} login` to sign in again.",
    connection_hint=_client_connection_hint,
    timeout_hint="Retry the command or check service logs if the operation keeps timing out.",
    debug_hint="Run the command again with `--debug` to see the full traceback.",
    classifiers=(_client_operation_classifier,),
)


__all__ = [
    "CliErrorPolicy",
    "ClientError",
    "ClientErrorDetails",
    "debug_errors_enabled",
    "exception_chain",
    "json_errors_enabled",
    "mask_secrets",
    "normalize_exception",
    "render_exception",
]

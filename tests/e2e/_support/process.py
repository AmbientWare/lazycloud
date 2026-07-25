from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from lazycloud.clients.resource.control import ResourceControlClient
from lazycloud.config import ClientProfile, get_profile
from shared.env import (
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)
from shared.http.errors import HttpTransportError

BLOCKED_EXIT = 77
DIAGNOSTIC_LIMIT = 1_000
_SENSITIVE_ENV_SUFFIXES = (
    "_ACCESS_KEY",
    "_ACCESS_KEY_ID",
    "_API_KEY",
    "_AUTHORIZATION",
    "_CREDENTIAL",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_SECRET",
    "_SECRET_ACCESS_KEY",
    "_SESSION_TOKEN",
    "_TOKEN",
)


class LivePrerequisiteError(RuntimeError):
    """A declared live-scenario prerequisite is unavailable."""


def require_live(
    argv: Sequence[str] | None,
    *,
    description: str,
    required_env: Sequence[str] = (),
) -> ClientProfile:
    """Require live opt-in and validate the active public client profile."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--live",
        action="store_true",
        help="mutate the already-prepared target environment",
    )
    args = parser.parse_args(argv)
    if not args.live:
        raise LivePrerequisiteError("this scenario requires the explicit --live opt-in")
    for name in (
        GATEWAY_HTTP_URL_ENV,
        GATEWAY_TOKEN_ENV,
        WORKSPACE_ID_ENV,
        WORKSPACE_NAME_ENV,
    ):
        os.environ.pop(name, None)
    profile = get_profile()
    if not profile.token:
        raise LivePrerequisiteError(
            "the active lazycloud profile has no token; run `lazycloud login` first"
        )
    missing = tuple(name for name in required_env if not os.getenv(name, "").strip())
    if missing:
        raise LivePrerequisiteError("missing required environment: " + ", ".join(sorted(missing)))
    try:
        ResourceControlClient.from_endpoint(
            profile.resolved_endpoint(),
            token=profile.token,
            timeout_seconds=10,
            workspace=profile.workspace,
        ).list_apps(active=True)
    except HttpTransportError as exc:
        raise LivePrerequisiteError(
            f"the configured control plane is unavailable: {profile.resolved_endpoint()}"
        ) from exc
    return profile


def blocked(error: LivePrerequisiteError) -> int:
    """Render a bounded blocked result without exposing environment values."""
    print(f"blocked: {error}", file=sys.stderr)
    return BLOCKED_EXIT


def run_text_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float = 300,
    check: bool = True,
    secrets: Sequence[str] = (),
) -> subprocess.CompletedProcess[str]:
    """Run one text subprocess with bounded, secret-safe failure diagnostics."""
    secret_values = _secret_values(environment, secrets)
    command_name = _command_name(command, secrets=secret_values)
    try:
        result: subprocess.CompletedProcess[str] = subprocess.run(
            tuple(command),
            cwd=cwd,
            env=dict(environment) if environment is not None else None,
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        detail = _failure_detail(
            stderr=exc.stderr,
            stdout=exc.stdout,
            fallback="no process output",
            secrets=secret_values,
        )
        raise RuntimeError(f"command timed out ({command_name}): {detail}") from None
    except OSError as exc:
        detail = _bounded_redacted_text(str(exc), secrets=secret_values)
        raise RuntimeError(f"command could not start ({command_name}): {detail}") from None
    if check and result.returncode != 0:
        detail = _failure_detail(
            stderr=result.stderr,
            stdout=result.stdout,
            fallback="no process output",
            secrets=secret_values,
        )
        raise RuntimeError(f"command failed ({command_name}, exit {result.returncode}): {detail}")
    return result


def redact_text(
    value: str,
    *,
    environment: Mapping[str, str] | None = None,
    secrets: Sequence[str] = (),
) -> str:
    """Redact explicit secrets and credential values from a child environment."""
    return _redact(value, _secret_values(environment, secrets))


def _command_name(command: Sequence[str], *, secrets: Sequence[str]) -> str:
    if not command:
        return "<empty>"
    return _bounded_redacted_text(command[0], secrets=secrets, limit=200)


def _failure_detail(
    *,
    stderr: str | bytes | None,
    stdout: str | bytes | None,
    fallback: str,
    secrets: Sequence[str],
) -> str:
    detail = _output_text(stderr).strip() or _output_text(stdout).strip() or fallback
    return _bounded_redacted_text(detail, secrets=secrets)


def _bounded_redacted_text(
    value: str,
    *,
    secrets: Sequence[str],
    limit: int = DIAGNOSTIC_LIMIT,
) -> str:
    return _redact(value, secrets)[-limit:]


def _redact(value: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        value = value.replace(secret, "<redacted>")
    return value


def _output_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _secret_values(
    environment: Mapping[str, str] | None,
    explicit: Sequence[str],
) -> tuple[str, ...]:
    child_environment = os.environ if environment is None else environment
    values = {secret for secret in explicit if secret}
    values.update(
        value
        for name, value in child_environment.items()
        if value and _is_sensitive_environment_name(name)
    )
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


def _is_sensitive_environment_name(name: str) -> bool:
    normalized = name.upper()
    return normalized in {"AUTHORIZATION", "CREDENTIALS"} or normalized.endswith(
        _SENSITIVE_ENV_SUFFIXES
    )


__all__ = [
    "BLOCKED_EXIT",
    "DIAGNOSTIC_LIMIT",
    "LivePrerequisiteError",
    "blocked",
    "redact_text",
    "require_live",
    "run_text_process",
]

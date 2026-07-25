"""Shared prerequisite gates and bounded polling for external E2E stages.

Every external stage runs against paid or shared external state, so the helpers
here enforce the stage contract: exit ``77`` when a prerequisite is absent, one
absolute deadline per stage, transient-tolerant polling, and machine-readable
JSON evidence on stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence

import httpx
from pydantic import BaseModel, Field
from shared.http.errors import HttpApiError, HttpTransportError

SKIP = 77


class MissingPrerequisite(RuntimeError):
    """The stage cannot run here; the caller exits ``SKIP`` instead of failing."""


class Deadline:
    """One absolute stage deadline shared by every wait in the stage."""

    def __init__(self, timeout_seconds: float) -> None:
        self._expires_at = time.monotonic() + timeout_seconds

    def expired(self) -> bool:
        return time.monotonic() >= self._expires_at

    def remaining_seconds(self) -> float:
        return max(0.0, self._expires_at - time.monotonic())


class AwsCallerIdentity(BaseModel):
    account_id: str = Field(alias="Account")


def skip(stage: str, reason: object) -> int:
    print(f"{stage} E2E prerequisite unavailable: {reason}", file=sys.stderr)
    return SKIP


def run_aws(arguments: Sequence[str]) -> object:
    """Run one read-only or corroborating ambient AWS CLI command."""
    result = subprocess.run(
        ["aws", *arguments],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        detail = stderr.splitlines()[-1][:500] if stderr else "no error detail"
        raise RuntimeError(f"the ambient AWS command failed: {detail}")
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def ambient_aws_account_id() -> str:
    if shutil.which("aws") is None:
        raise MissingPrerequisite("the AWS CLI is not installed")
    result = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--output", "json"],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "AWS_PAGER": ""},
    )
    if result.returncode != 0:
        raise MissingPrerequisite("ambient AWS credentials are unavailable")
    try:
        return AwsCallerIdentity.model_validate_json(result.stdout).account_id
    except ValueError:
        raise MissingPrerequisite("ambient AWS identity returned no account ID") from None


def prepared_gateway(*, token_variable: str = "LAZYCLOUD_TOKEN") -> tuple[str, str, str]:
    """Return the prepared public endpoint, token, and workspace or raise a skip."""
    endpoint = os.getenv("LAZYCLOUD_ENDPOINT", "").rstrip("/")
    token = os.getenv(token_variable, "")
    workspace = os.getenv("LAZYCLOUD_WORKSPACE", "default")
    if not endpoint or not token:
        raise MissingPrerequisite(f"LAZYCLOUD_ENDPOINT and {token_variable} are required")
    try:
        httpx.get(f"{endpoint}/health", timeout=10).raise_for_status()
    except httpx.HTTPError as exc:
        raise MissingPrerequisite(f"the prepared gateway is unavailable: {exc}") from exc
    return endpoint, token, workspace


def first_public_call[T](fetch: Callable[[], T]) -> T:
    """Convert unreachable/unauthenticated public control planes into skips."""
    try:
        return fetch()
    except HttpTransportError as exc:
        raise MissingPrerequisite(f"public control plane is unreachable: {exc}") from exc
    except HttpApiError as exc:
        if exc.status_code != 401:
            raise
        raise MissingPrerequisite("public authentication failed") from exc


def poll_until[T](
    deadline: Deadline,
    waiting_for: str,
    check: Callable[[], T | None],
    *,
    interval_seconds: float = 3.0,
) -> T:
    """Poll ``check`` until it returns a value or the stage deadline expires.

    ``check`` returns ``None`` while the awaited state has not converged and
    raises for terminal failures. Transient public transport errors do not
    fail the stage; only the deadline does.
    """
    last_transient: HttpTransportError | None = None
    while True:
        try:
            value = check()
        except HttpTransportError as exc:
            last_transient = exc
            value = None
        if value is not None:
            return value
        if deadline.expired():
            suffix = f"; last transport error: {last_transient}" if last_transient else ""
            raise RuntimeError(f"timed out waiting for {waiting_for}{suffix}")
        time.sleep(min(interval_seconds, max(0.1, deadline.remaining_seconds())))


def emit_evidence(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


__all__ = [
    "SKIP",
    "AwsCallerIdentity",
    "Deadline",
    "MissingPrerequisite",
    "ambient_aws_account_id",
    "emit_evidence",
    "first_public_call",
    "poll_until",
    "prepared_gateway",
    "run_aws",
    "skip",
]

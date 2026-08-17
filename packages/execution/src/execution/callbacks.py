from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import socket
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import SplitResult, urlsplit

from control.service import ControlPlaneService
from identity.signatures import sign_payload
from observability.events import EventService
from shared.callbacks import normalize_callback_url
from shared.deployments import StubKind
from shared.errors import NotFoundError
from shared.events import EventLevel
from shared.http.callbacks import TaskCallbackBody
from shared.tasks import Task, TaskStatus, is_terminal_task_status

from execution.context import ExecutionContext

CALLBACK_DELIVERY_ATTEMPTS = 3
CALLBACK_RETRY_DELAYS_SECONDS: tuple[float, ...] = (0.25, 0.75)
CALLBACK_REQUEST_TIMEOUT_SECONDS = 5.0
CALLBACK_RESPONSE_BODY_LIMIT = 64 * 1024
CALLBACK_SUPPORTED_STUB_KINDS: frozenset[StubKind] = frozenset(
    {
        StubKind.Function,
        StubKind.CronJob,
        StubKind.Endpoint,
        StubKind.Asgi,
    }
)


class TaskCallbackSender(Protocol):
    def send(self, target: str, body: bytes, headers: Mapping[str, str]) -> int: ...


class TaskCallbackDispatcher(Protocol):
    def deliver(self, task: Task) -> None: ...


class CallbackDeliveryError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        resolved_ip: str,
        *,
        timeout_seconds: float,
    ) -> None:
        tls_context = ssl.create_default_context()
        super().__init__(
            hostname,
            port=port,
            timeout=timeout_seconds,
            context=tls_context,
        )
        self.resolved_ip = resolved_ip
        self.tls_context = tls_context

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self.resolved_ip, self.port),
            self.timeout,
        )
        self.sock = self.tls_context.wrap_socket(raw_socket, server_hostname=self.host)


@dataclass(frozen=True, slots=True)
class HttpTaskCallbackSender:
    timeout_seconds: float = CALLBACK_REQUEST_TIMEOUT_SECONDS

    def send(self, target: str, body: bytes, headers: Mapping[str, str]) -> int:
        parsed = _validated_target(target)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        resolved_ip = _public_target_ip(parsed.hostname or "", port)
        connection: http.client.HTTPConnection
        if parsed.scheme == "https":
            connection = _PinnedHttpsConnection(
                parsed.hostname or "",
                port,
                resolved_ip,
                timeout_seconds=self.timeout_seconds,
            )
        else:
            connection = http.client.HTTPConnection(
                resolved_ip,
                port=port,
                timeout=self.timeout_seconds,
            )
        request_headers = dict(headers)
        request_headers["Host"] = _host_header(parsed, port)
        try:
            connection.request(
                "POST",
                _request_target(parsed),
                body=body,
                headers=request_headers,
            )
            response = connection.getresponse()
            response.read(CALLBACK_RESPONSE_BODY_LIMIT)
            status = response.status
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise CallbackDeliveryError(
                f"callback transport failed: {type(exc).__name__}",
                retryable=True,
            ) from exc
        finally:
            connection.close()
        if 200 <= status < 300:
            return status
        raise CallbackDeliveryError(
            f"callback returned HTTP {status}",
            retryable=status in {408, 425, 429} or status >= 500,
        )


@dataclass(slots=True)
class TaskCallbackService:
    context: ExecutionContext
    events: EventService
    sender: TaskCallbackSender = field(default_factory=HttpTaskCallbackSender)
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.time

    def deliver(self, task: Task) -> None:
        if task.status is not TaskStatus.Retry and not is_terminal_task_status(task.status):
            return
        target = self._target(task)
        if target is None:
            return
        payload = _callback_body(task)
        body = json.dumps(
            payload.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        timestamp = int(self.now())
        signature = sign_payload(body, self._signing_key(task), timestamp=timestamp)
        idempotency_key = _callback_idempotency_key(task)
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
            "X-Task-ID": task.id,
            "X-Task-Status": task.status.value,
            "X-Task-Attempt": str(task.attempt_number),
            "X-Task-Signature": signature.key,
            "X-Task-Timestamp": str(signature.timestamp),
        }
        attempts = 0
        last_error = "callback delivery failed"
        for attempts in range(1, CALLBACK_DELIVERY_ATTEMPTS + 1):
            try:
                status_code = self.sender.send(target, body, headers)
            except CallbackDeliveryError as exc:
                last_error = str(exc)
                if not exc.retryable or attempts >= CALLBACK_DELIVERY_ATTEMPTS:
                    break
                self.sleep(CALLBACK_RETRY_DELAYS_SECONDS[attempts - 1])
                continue
            except Exception as exc:
                last_error = f"callback transport failed: {type(exc).__name__}"
                if attempts >= CALLBACK_DELIVERY_ATTEMPTS:
                    break
                self.sleep(CALLBACK_RETRY_DELAYS_SECONDS[attempts - 1])
                continue
            self.events.emit(
                "task.callback.delivered",
                resource_type="task",
                resource_id=task.id,
                message="task callback delivered",
                data={
                    "attempts": attempts,
                    "callback_host": urlsplit(target).hostname or "",
                    "idempotency_key": idempotency_key,
                    "status_code": status_code,
                    "task_status": task.status.value,
                },
                workspace_id=task.workspace_id,
            )
            return
        self.events.emit(
            "task.callback.failed",
            resource_type="task",
            resource_id=task.id,
            message="task callback delivery failed",
            level=EventLevel.Warning,
            data={
                "attempts": attempts,
                "callback_host": urlsplit(target).hostname or "",
                "error": last_error,
                "idempotency_key": idempotency_key,
                "task_status": task.status.value,
            },
            workspace_id=task.workspace_id,
        )

    def _target(self, task: Task) -> str | None:
        if not task.stub_id:
            return None
        try:
            stub = ControlPlaneService(self.context).get_stub(task.stub_id)
        except NotFoundError:
            return None
        if stub.kind not in CALLBACK_SUPPORTED_STUB_KINDS:
            return None
        return normalize_callback_url(stub.config.callback_url)

    def _signing_key(self, task: Task) -> str:
        if not task.workspace_id:
            msg = f"task {task.id} has no workspace"
            raise CallbackDeliveryError(msg, retryable=False)
        return ControlPlaneService(self.context).workspace_signing_key(task.workspace_id)


def _callback_body(task: Task) -> TaskCallbackBody:
    return TaskCallbackBody(
        task_id=task.id,
        root_task_id=task.root_task_id or task.id,
        status=task.status,
        attempt_number=task.attempt_number,
        max_attempts=task.max_attempts,
        retry_scheduled=task.status is TaskStatus.Retry,
        data=(
            task.function_result.model_dump(mode="json")
            if task.function_result is not None
            else task.result
        ),
        error=task.error,
        finished_at=task.finished_at,
    )


def _callback_idempotency_key(task: Task) -> str:
    identity = f"{task.id}:{task.attempt_number}:{task.status.value}".encode()
    return hashlib.sha256(identity).hexdigest()


def _validated_target(target: str) -> SplitResult:
    normalized = normalize_callback_url(target)
    if normalized is None:
        raise CallbackDeliveryError("callback target is empty", retryable=False)
    return urlsplit(normalized)


def _public_target_ip(hostname: str, port: int) -> str:
    try:
        addresses = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise CallbackDeliveryError(
            "callback hostname could not be resolved",
            retryable=True,
        ) from exc
    resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for address in addresses:
        candidate = ipaddress.ip_address(address[4][0])
        if candidate not in resolved:
            resolved.append(candidate)
    if not resolved:
        raise CallbackDeliveryError("callback hostname has no addresses", retryable=True)
    if any(not candidate.is_global for candidate in resolved):
        raise CallbackDeliveryError(
            "callback target resolves to a non-public address",
            retryable=False,
        )
    return str(resolved[0])


def _request_target(parsed: SplitResult) -> str:
    path = parsed.path or "/"
    return f"{path}?{parsed.query}" if parsed.query else path


def _host_header(parsed: SplitResult, port: int) -> str:
    hostname = parsed.hostname or ""
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    return hostname if port == default_port else f"{hostname}:{port}"


__all__ = [
    "CallbackDeliveryError",
    "HttpTaskCallbackSender",
    "TaskCallbackDispatcher",
    "TaskCallbackSender",
    "TaskCallbackService",
]

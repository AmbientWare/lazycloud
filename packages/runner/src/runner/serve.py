from __future__ import annotations

import contextlib
import inspect
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process
from types import FrameType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import uvicorn
from foundation.handler_loading import evict_user_code_modules, load_callable
from pydantic import TypeAdapter, ValidationError
from shared.container_requests import CONTAINER_HEALTH_PATH, CONTAINER_INNER_PORT
from shared.deployments import DeploymentKind
from shared.env import (
    APP_ID_ENV,
    CHECKPOINT_ENABLED_ENV,
    CONTAINER_HOSTNAME_ENV,
    CONTAINER_ID_ENV,
    ENDPOINT_SERVE_HOST_ENV,
    ENDPOINT_WORKERS_ENV,
    GATEWAY_HTTP_URL_ENV,
    GATEWAY_TOKEN_ENV,
    LIFECYCLE_HOOKS_ENV,
    STUB_ID_ENV,
    STUB_TYPE_ENV,
    WORKSPACE_ID_ENV,
    WORKSPACE_NAME_ENV,
)
from shared.http.endpoint_forwarding import ASGIMessage, ASGIReceive, ASGISend
from shared.http.endpoints import EndpointForwardRequest, EndpointForwardResponse
from shared.http.task_payload import serialize_http_task_payload
from shared.http_transport import HttpChannel
from shared.lifecycle import (
    LifecycleHookName,
    LifecycleHooks,
    LifecycleStartupContext,
)

from runner.checkpoints import wait_for_checkpoint
from runner.endpoint_forwarding import (
    call_asgi_app,
    error_response,
    resolve_endpoint_result,
    response_from_endpoint_result,
)
from runner.hooks import lifecycle_hooks_from_env, run_lifecycle_hooks
from runner.invocation import invoke_handler
from runner.reload import SourceChangeWatcher, hot_reload_enabled, hot_reload_root
from runner.runtime import DEFAULT_GATEWAY_ENDPOINT, DEFAULT_RUNNER_TIMEOUT_SECONDS, post_task_log
from runner.worker_processes import stop_worker_processes

ENDPOINT_SERVE_PORT_ENV = "BIND_PORT"
ENDPOINT_HANDLER_ENV = "HANDLER"
ASGI_STUB_TYPE = "asgi"
_ASGI_HEADERS_ADAPTER = TypeAdapter(list[tuple[bytes, bytes]])


class _SharedPortThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    allow_reuse_port = True


@dataclass(slots=True)
class EndpointServeRunner:
    handler_ref: str
    stub_type: str = "endpoint"
    host: str = "0.0.0.0"
    port: int = CONTAINER_INNER_PORT
    stub_id: str = ""
    workspace_id: str = ""
    workspace_name: str = ""
    app_id: str = ""
    container_id: str = ""
    container_hostname: str = ""
    endpoint: str = DEFAULT_GATEWAY_ENDPOINT
    token: str = ""
    timeout_seconds: float = DEFAULT_RUNNER_TIMEOUT_SECONDS
    lifecycle_hooks: LifecycleHooks = field(default_factory=LifecycleHooks)
    checkpoint_enabled: bool = False
    workers: int = 1
    channel: HttpChannel | None = None
    _handler: Callable[..., Any] | None = field(default=None, init=False)
    _handler_lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    @property
    def is_asgi(self) -> bool:
        return self.stub_type.strip().lower() == ASGI_STUB_TYPE

    def handler(self) -> Callable[..., Any]:
        with self._handler_lock:
            if self._handler is None:
                if not self.handler_ref:
                    msg = "endpoint handler is not configured"
                    raise RuntimeError(msg)
                self._handler = load_callable(self.handler_ref)
            return self._handler

    @property
    def control(self) -> HttpChannel:
        if self.channel is None:
            self.channel = HttpChannel(
                endpoint=self.endpoint,
                token=self.token or None,
                timeout_seconds=self.timeout_seconds,
            )
        return self.channel

    def reload_handler(self) -> None:
        root = hot_reload_root()
        with self._handler_lock:
            evict_user_code_modules(root)
            self._handler = None
        print("hot reload: endpoint handler refreshed", flush=True)

    def create_server(self) -> ThreadingHTTPServer:
        runner = self

        class Handler(_EndpointServeHTTPHandler):
            endpoint_runner = runner

        return _SharedPortThreadingHTTPServer((self.host, self.port), Handler)

    def serve_forever(self) -> None:
        if self.is_asgi:
            self.run_startup_hooks()
            uvicorn.run(
                RunnerASGIApplication(self),
                host=self.host,
                port=self.port,
                log_level="info",
            )
            return
        server = self.create_server()
        watcher = (
            SourceChangeWatcher(hot_reload_root(), self.reload_handler)
            if hot_reload_enabled()
            else None
        )
        if watcher is not None:
            watcher.start()
        try:
            self.run_startup_hooks()
            server.serve_forever()
        finally:
            if watcher is not None:
                watcher.stop()
            server.server_close()

    def handle(self, request: EndpointForwardRequest) -> EndpointForwardResponse:
        try:
            if self.is_asgi:
                return call_asgi_app(self.handler(), request)
            return self._handle_function_endpoint(request)
        except Exception as exc:
            formatted = traceback.format_exc()
            task_id = _task_id(request)
            if task_id:
                with contextlib.suppress(Exception):
                    self.append_task_log(task_id, "stderr", formatted)
            print(formatted, file=sys.stderr, end="", flush=True)
            return error_response(500, f"{type(exc).__name__}: {exc}")

    def _handle_function_endpoint(
        self,
        request: EndpointForwardRequest,
    ) -> EndpointForwardResponse:
        try:
            payload = serialize_http_task_payload(
                request.body,
                query_params=request.query_params,
            )
        except ValueError as exc:
            return error_response(400, str(exc))
        result = invoke_handler(self.handler(), *(payload.args or []), **payload.kwargs)
        return response_from_endpoint_result(resolve_endpoint_result(result))

    def append_task_log(self, task_id: str, stream: str, message: str) -> None:
        post_task_log(self.control, task_id, stream, message)

    def run_startup_hooks(self) -> None:
        self.handler()
        context = LifecycleStartupContext(
            stub_id=self.stub_id,
            workspace_id=self.workspace_id,
            workspace_name=self.workspace_name,
            app_id=self.app_id,
            container_id=self.container_id,
            container_hostname=self.container_hostname,
            handler=self.handler_ref,
            resource_kind=DeploymentKind.Asgi if self.is_asgi else DeploymentKind.Endpoint,
        )
        run_lifecycle_hooks(
            self.lifecycle_hooks,
            LifecycleHookName.Start,
            context,
            log=_container_log,
            capture_output=False,
        )
        restored = wait_for_checkpoint(
            enabled=self.checkpoint_enabled,
            workers=self.workers,
        )
        if restored is not None:
            self.container_id = restored.container_id
            self.container_hostname = restored.container_hostname


@dataclass(slots=True)
class RunnerASGIApplication:
    runner: EndpointServeRunner

    async def __call__(
        self,
        scope: ASGIMessage,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") == "http" and scope.get("path") == CONTAINER_HEALTH_PATH:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain; charset=utf-8")],
                }
            )
            await send({"type": "http.response.body", "body": b"ok"})
            return
        try:
            result = self.runner.handler()(scope, receive, send)
            if inspect.isawaitable(result):
                await result
        except Exception:
            formatted = traceback.format_exc()
            task_id = _asgi_task_id(scope)
            if task_id:
                with contextlib.suppress(Exception):
                    self.runner.append_task_log(task_id, "stderr", formatted)
            print(formatted, file=sys.stderr, end="", flush=True)
            raise


class _EndpointServeHTTPHandler(BaseHTTPRequestHandler):
    endpoint_runner: EndpointServeRunner
    server_version = "RunnerEndpointServe/1.0"

    def do_CONNECT(self) -> None:
        self._serve()

    def do_DELETE(self) -> None:
        self._serve()

    def do_GET(self) -> None:
        self._serve()

    def do_HEAD(self) -> None:
        self._serve(send_body=False)

    def do_OPTIONS(self) -> None:
        self._serve()

    def do_PATCH(self) -> None:
        self._serve()

    def do_POST(self) -> None:
        self._serve()

    def do_PUT(self) -> None:
        self._serve()

    def do_TRACE(self) -> None:
        self._serve()

    def log_message(self, format: str, *args: str | int | float) -> None:
        _ = format, args

    def _serve(self, *, send_body: bool = True) -> None:
        path = urlsplit(self.path).path or "/"
        if path == CONTAINER_HEALTH_PATH:
            self._write(EndpointForwardResponse(body=b"ok"), send_body=send_body)
            return
        request = EndpointForwardRequest(
            stub_id=os.getenv(STUB_ID_ENV, "served-endpoint"),
            method=self.command,
            path=path,
            query_params=parse_qs(urlsplit(self.path).query, keep_blank_values=True),
            headers=_headers_from_request(self),
            body=self._body(),
        )
        self._write(self.endpoint_runner.handle(request), send_body=send_body)

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _write(self, response: EndpointForwardResponse, *, send_body: bool) -> None:
        self.send_response(response.status_code)
        has_content_length = False
        for key, values in response.headers.items():
            for value in values:
                if key.lower() == "content-length":
                    has_content_length = True
                self.send_header(key, value)
        if not has_content_length:
            self.send_header("content-length", str(len(response.body)))
        self.end_headers()
        if send_body and response.body:
            self.wfile.write(response.body)


def run_endpoint_serve_forever(
    *,
    handler_ref: str | None = None,
    stub_type: str | None = None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    effective_stub_type = stub_type if stub_type is not None else os.getenv(STUB_TYPE_ENV, "")
    effective_host = host if host is not None else os.getenv(ENDPOINT_SERVE_HOST_ENV, "0.0.0.0")
    effective_port = port if port is not None else _env_port()
    if effective_stub_type.strip().lower() == ASGI_STUB_TYPE:
        uvicorn.run(
            "runner.serve:create_asgi_application",
            factory=True,
            host=effective_host,
            port=effective_port,
            workers=_env_workers(),
            log_level="info",
        )
        return
    EndpointProcessManager(
        handler_ref=(
            handler_ref if handler_ref is not None else required_env(ENDPOINT_HANDLER_ENV)
        ),
        stub_type=effective_stub_type,
        host=effective_host,
        port=effective_port,
        workers=_env_workers(),
    ).run()


@dataclass(slots=True)
class EndpointProcessManager:
    handler_ref: str
    stub_type: str
    host: str
    port: int
    workers: int
    poll_interval_seconds: float = 0.1
    shutdown: threading.Event = field(default_factory=threading.Event)
    processes: list[Process] = field(default_factory=list, init=False)

    def run(self) -> None:
        previous_handlers = {
            handled_signal: signal.signal(handled_signal, self._request_shutdown)
            for handled_signal in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            for index in range(self.workers):
                self.processes.append(self._start_worker(index))
            while not self.shutdown.wait(self.poll_interval_seconds):
                failed = next(
                    (process for process in self.processes if process.exitcode is not None),
                    None,
                )
                if failed is not None:
                    msg = f"endpoint worker process exited with {failed.exitcode}"
                    raise RuntimeError(msg)
        finally:
            self.stop()
            for handled_signal, previous_handler in previous_handlers.items():
                signal.signal(handled_signal, previous_handler)

    def stop(self) -> None:
        stop_worker_processes(self.shutdown, self.processes)

    def _start_worker(self, index: int) -> Process:
        process = Process(
            target=_run_function_endpoint_worker,
            args=(
                self.handler_ref,
                self.stub_type,
                self.host,
                self.port,
                self.workers,
            ),
            name=f"endpoint-worker-{index}",
        )
        process.start()
        return process

    def _request_shutdown(self, signum: int, frame: FrameType | None) -> None:
        del signum, frame
        self.shutdown.set()


def _run_function_endpoint_worker(
    handler_ref: str,
    stub_type: str,
    host: str,
    port: int,
    workers: int,
) -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    EndpointServeRunner(
        handler_ref=handler_ref,
        stub_type=stub_type,
        host=host,
        port=port,
        stub_id=os.getenv(STUB_ID_ENV, ""),
        workspace_id=os.getenv(WORKSPACE_ID_ENV, ""),
        workspace_name=os.getenv(WORKSPACE_NAME_ENV, ""),
        app_id=os.getenv(APP_ID_ENV, ""),
        container_id=os.getenv(CONTAINER_ID_ENV, ""),
        container_hostname=os.getenv(CONTAINER_HOSTNAME_ENV, ""),
        endpoint=os.getenv(GATEWAY_HTTP_URL_ENV, DEFAULT_GATEWAY_ENDPOINT),
        token=os.getenv(GATEWAY_TOKEN_ENV, ""),
        lifecycle_hooks=lifecycle_hooks_from_env(os.getenv(LIFECYCLE_HOOKS_ENV)),
        checkpoint_enabled=_env_bool(CHECKPOINT_ENABLED_ENV),
        workers=workers,
    ).serve_forever()


def create_asgi_application() -> RunnerASGIApplication:
    runner = EndpointServeRunner(
        handler_ref=required_env(ENDPOINT_HANDLER_ENV),
        stub_type=ASGI_STUB_TYPE,
        host=os.getenv(ENDPOINT_SERVE_HOST_ENV, "0.0.0.0"),
        port=_env_port(),
        stub_id=os.getenv(STUB_ID_ENV, ""),
        workspace_id=os.getenv(WORKSPACE_ID_ENV, ""),
        workspace_name=os.getenv(WORKSPACE_NAME_ENV, ""),
        app_id=os.getenv(APP_ID_ENV, ""),
        container_id=os.getenv(CONTAINER_ID_ENV, ""),
        container_hostname=os.getenv(CONTAINER_HOSTNAME_ENV, ""),
        endpoint=os.getenv(GATEWAY_HTTP_URL_ENV, DEFAULT_GATEWAY_ENDPOINT),
        token=os.getenv(GATEWAY_TOKEN_ENV, ""),
        lifecycle_hooks=lifecycle_hooks_from_env(os.getenv(LIFECYCLE_HOOKS_ENV)),
        checkpoint_enabled=_env_bool(CHECKPOINT_ENABLED_ENV),
        workers=_env_workers(),
    )
    runner.run_startup_hooks()
    return RunnerASGIApplication(runner)


def required_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        msg = f"{name} is required"
        raise RuntimeError(msg)
    return value


def _task_id(request: EndpointForwardRequest) -> str:
    for name, values in request.headers.items():
        if name.lower() == "x-task-id" and values:
            return values[0].strip()
    return ""


def _asgi_task_id(scope: ASGIMessage) -> str:
    try:
        headers = _ASGI_HEADERS_ADAPTER.validate_python(scope.get("headers"))
    except ValidationError:
        return ""
    for key, value in headers:
        if key.lower() == b"x-task-id":
            return value.decode("latin-1").strip()
    return ""


def _container_log(stream: str, message: str) -> None:
    if stream == "stderr":
        print(message, file=sys.stderr, end="", flush=True)
    else:
        print(message, end="", flush=True)


def _headers_from_request(handler: BaseHTTPRequestHandler) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for key in handler.headers:
        headers[key] = handler.headers.get_all(key, [])
    return headers


def _env_port() -> int:
    raw = os.getenv(ENDPOINT_SERVE_PORT_ENV, str(CONTAINER_INNER_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        msg = f"{ENDPOINT_SERVE_PORT_ENV} must be an integer"
        raise RuntimeError(msg) from exc
    if not 1 <= port <= 65535:
        msg = f"{ENDPOINT_SERVE_PORT_ENV} must be between 1 and 65535"
        raise RuntimeError(msg)
    return port


def _env_workers() -> int:
    raw = os.getenv(ENDPOINT_WORKERS_ENV, "1")
    try:
        workers = int(raw)
    except ValueError as exc:
        msg = f"{ENDPOINT_WORKERS_ENV} must be an integer"
        raise RuntimeError(msg) from exc
    if workers < 1:
        msg = f"{ENDPOINT_WORKERS_ENV} must be at least 1"
        raise RuntimeError(msg)
    return workers


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    run_endpoint_serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from urllib.parse import urlencode

from pydantic import BaseModel
from shared.function_payloads import FunctionInvocationPayload, FunctionResultPayload
from shared.http.errors import HttpResponseDecodeError
from shared.http.functions import (
    FunctionCallDependency,
    FunctionCronRequest,
    FunctionCronResponse,
    FunctionInvokeBody,
    FunctionInvokeResponse,
    FunctionMonitorRequest,
    FunctionMonitorResponse,
    FunctionSetResultBody,
    FunctionSetResultResponse,
)
from shared.http_transport import HttpChannel


class FunctionControlChannel(Protocol):
    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any: ...

    def stream_post(self, path: str, payload: dict[str, Any] | None = None) -> Iterator[Any]: ...


ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass
class FunctionControlClient:
    channel: FunctionControlChannel
    workspace: str = "default"
    """Workspace every call acts in, named rather than inferred.

    A user credential reaches every workspace its owner belongs to, so the request
    has to say which one; inside a container the workspace comes from the environment
    the runner pins. Either way the caller states it rather than letting the server
    pick one.
    """

    @classmethod
    def from_endpoint(
        cls,
        endpoint: str,
        *,
        token: str | None = None,
        workspace: str = "default",
        timeout_seconds: float = 10.0,
    ) -> FunctionControlClient:
        return cls(
            channel=HttpChannel(endpoint=endpoint, token=token, timeout_seconds=timeout_seconds),
            workspace=workspace,
        )

    def _scoped(self, path: str) -> str:
        separator = "&" if "?" in path else "?"
        return f"{path}{separator}{urlencode({'workspace': self.workspace})}"

    def invoke(
        self,
        stub_id: str,
        invocation: FunctionInvocationPayload,
        *,
        detached: bool = False,
        parent_task_id: str = "",
        root_task_id: str = "",
        dependencies: list[FunctionCallDependency] | None = None,
    ) -> Iterator[FunctionInvokeResponse]:
        body = FunctionInvokeBody(
            stub_id=stub_id,
            headless=detached,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            dependencies=dependencies or [],
            invocation=invocation,
        )
        for item in self.channel.stream_post(
            self._scoped("/api/v1/functions/invoke/stream"),
            body.model_dump(mode="json"),
        ):
            yield _validate_response(FunctionInvokeResponse, item)


    def set_result(
        self,
        task_id: str,
        container_id: str,
        result: FunctionResultPayload,
    ) -> FunctionSetResultResponse:
        body = FunctionSetResultBody(
            task_id=task_id,
            container_id=container_id,
            result=result,
        )
        return _validate_response(
            FunctionSetResultResponse,
            self.channel.post(
                self._scoped("/api/v1/functions/set-result"), body.model_dump(mode="json")
            ),
        )

    def monitor_once(
        self,
        task_id: str,
        stub_id: str,
        *,
        container_id: str = "",
    ) -> FunctionMonitorResponse:
        return _validate_response(
            FunctionMonitorResponse,
            self.channel.post(
                self._scoped("/api/v1/functions/monitor"),
                FunctionMonitorRequest(
                    task_id=task_id,
                    stub_id=stub_id,
                    container_id=container_id,
                ).model_dump(mode="json"),
            ),
        )

    def cron(
        self,
        stub_id: str,
        cron: str,
        deployment_id: str,
    ) -> FunctionCronResponse:
        return _validate_response(
            FunctionCronResponse,
            self.channel.post(
                self._scoped("/api/v1/functions/cron"),
                FunctionCronRequest(
                    stub_id=stub_id,
                    cron=cron,
                    deployment_id=deployment_id,
                ).model_dump(mode="json"),
            ),
        )


def _validate_response(model: type[ResponseT], value: object) -> ResponseT:
    try:
        return model.model_validate(value)
    except ValueError as exc:
        raise HttpResponseDecodeError("function control returned an invalid response") from exc


__all__ = [
    "FunctionControlChannel",
    "FunctionControlClient",
]

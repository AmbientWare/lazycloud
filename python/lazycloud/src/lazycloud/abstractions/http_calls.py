"""Definitions, targets and requests of endpoints, ASGI and realtime apps."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, cast
from urllib.parse import urlencode

import httpx
from pydantic import JsonValue, ValidationError
from shared.api import FunctionSpec as ApiFunctionSpec
from shared.autoscaling import Autoscaler
from shared.image_building.python import python_minor_version

from lazycloud.abstractions.metadata import lifecycle_hook_references
from lazycloud.clients.endpoints import get_http_workload
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.env import is_local
from lazycloud.exceptions import SdkError
from lazycloud.json_contracts import parse_json_value

if TYPE_CHECKING:
    from lazycloud.abstractions.image import ImageBuildResult

InvocationTargetName: TypeAlias = Literal["auto", "served", "deployed"]
INVOCATION_TARGETS = ("auto", "served", "deployed")

# Added to the workload's timeout so the platform answers before the read gives up.
_REQUEST_MARGIN_SECONDS = 30.0


class InvocationTargetError(SdkError):
    pass


@dataclass(frozen=True, slots=True)
class InvocationOptions:
    target: InvocationTargetName = "auto"
    deployment_name: str | None = None
    deployment_version: int | None = None


@dataclass(frozen=True, slots=True)
class EndpointResponse:
    status_code: int
    headers: Mapping[str, list[str]]
    content: bytes
    url: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> JsonValue:
        return parse_json_value(self.text)


def http_function_spec(
    owner: Any,
    *,
    kind: str,
    handler: str,
    source_sha256: str,
    image: ImageBuildResult,
    concurrency: int,
    keep_warm: int | None,
    max_pending_tasks: int | None,
    retry_policy: Any = None,
    route: str | None = None,
    methods: list[str] | None = None,
) -> ApiFunctionSpec:
    """The API definition of an HTTP workload for an uploaded source and a ready image."""
    from lazycloud.abstractions.function import _resources, _volume_spec

    http: dict[str, Any] = {
        "kind": kind,
        "workers": owner.workers,
    }
    if route is not None:
        http["route"] = "/" + route.lstrip("/")
    if methods is not None:
        http["methods"] = [method.strip().upper() for method in methods if method.strip()]
    if owner.domain:
        http["domain"] = owner.domain.strip().rstrip(".").lower()
    spec: dict[str, Any] = {
        "name": owner.resource_name,
        "handler": handler,
        "source": {"sha256": source_sha256},
        "image": {
            "python_version": python_minor_version(image.python_version),
            "image_id": image.image_id,
        },
        "resources": _resources(owner.cpu, owner.memory, owner.disk),
        "concurrency": concurrency,
        "http": http,
    }
    if owner.effective_timeout_seconds() is not None:
        spec["timeout_seconds"] = owner.effective_timeout_seconds()
    if keep_warm is not None:
        spec["keep_warm_seconds"] = keep_warm
    if max_pending_tasks is not None:
        spec["max_pending_tasks"] = max_pending_tasks
    if owner.autoscaler is not None:
        spec["autoscaler"] = Autoscaler.model_validate(owner.autoscaler).model_dump(
            include={"min_containers", "max_containers", "tasks_per_container"}
        )
    if retry_policy is not None:
        spec["retry_policy"] = retry_policy.model_dump(
            mode="json",
            include={"max_attempts", "delay_seconds", "backoff", "max_delay_seconds"},
            exclude_none=True,
        )
    if owner.env:
        spec["environment"] = dict(owner.env)
    if owner.secrets:
        spec["secrets"] = list(dict.fromkeys(owner.secrets))
    if owner.volumes:
        spec["volumes"] = [_volume_spec(volume) for volume in owner.volumes]
    if owner.authorized is False:
        spec["authorized"] = False
    try:
        on_start = lifecycle_hook_references(owner.on_start)
    except (TypeError, ValueError) as exc:
        from lazycloud.abstractions.function import FunctionOperationError

        msg = f"{kind} {owner.resource_name} has invalid options: {exc}"
        raise FunctionOperationError(msg) from exc
    if on_start:
        # Workers run on_start once each, before they take requests.
        spec["lifecycle_hooks"] = {"on_start": list(on_start)}
    try:
        return ApiFunctionSpec.model_validate(spec)
    except ValidationError as exc:
        from lazycloud.abstractions.function import FunctionOperationError

        msg = f"{kind} {owner.resource_name} has invalid options: {exc}"
        raise FunctionOperationError(msg) from exc


def unsupported_http_options(owner: Any) -> list[str]:
    """Declared options HTTP workloads cannot run yet, by name."""
    found: list[str] = []
    if owner.gpu is not None or owner.gpu_count:
        found.append("gpu")
    declared: dict[str, bool] = {
        # Hosts have no credentials of their own for a user's bucket.
        "cloud bucket without key secrets": any(
            volume.config is not None and volume.config.get("auth_mode") != "secret_references"
            for volume in owner.volumes
        ),
        "callback_url": bool(owner.callback_url),
        "checkpoint_enabled": bool(owner.checkpoint_enabled),
        "docker_enabled": bool(getattr(owner, "docker_enabled", False)),
        "region": owner.region is not None,
        "availability_zone": bool(owner.availability_zone),
        "machine": owner.machine is not None,
        "metadata": bool(getattr(owner, "metadata", None)),
    }
    found.extend(name for name, present in declared.items() if present)
    return found


def resolve_url(
    owner: Any, *, kind: str, options: InvocationOptions
) -> tuple[str, str | None, float]:
    """Where a request goes, the token to send, and the read timeout.

    A running preview this machine started wins unless the target is
    `deployed`; `served` never falls back to the deployment. A public target
    gets no token: the platform credential never reaches a workload.
    """
    from lazycloud.abstractions.serve import read_serve_preview

    target = options.target.strip().lower()
    if target not in INVOCATION_TARGETS:
        raise InvocationTargetError(f"target must be one of: {', '.join(INVOCATION_TARGETS)}")
    if options.deployment_version is not None and target == "served":
        raise InvocationTargetError(
            "deployment_version can only be used with auto or deployed targets"
        )
    config = resolve_control_client_config(timeout_seconds=30)
    client = api_client(config)
    workspace = require_workspace(config)
    timeout = (owner.effective_timeout_seconds() or 180) + _REQUEST_MARGIN_SECONDS
    name = owner.resource_name
    if target in {"auto", "served"} and options.deployment_version is None and is_local():
        record = read_serve_preview(
            kind=kind,
            name=name,
            app=owner._app_slug,
            workspace=workspace,
            endpoint=config.endpoint,
            client=client,
        )
        if record is not None:
            # The preview runs this definition.
            token = config.token if owner.authorized is not False else None
            return record.url, token, timeout
        if target == "served":
            raise InvocationTargetError(f"no active served {kind} target found for {name}")
    workload = get_http_workload(
        client,
        workspace,
        owner._app_slug,
        options.deployment_name or name,
        asgi=kind != "endpoint",
        version=options.deployment_version,
    )
    url = workload.version_url if options.deployment_version is not None else workload.url
    token = config.token if workload.release.spec.authorized is not False else None
    return url, token, timeout


def send_request(
    url: str,
    *,
    method: str,
    path: str = "",
    json_body: object | None = None,
    data: bytes | str | None = None,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, object] | Iterable[tuple[str, object]] | None = None,
    token: str | None,
    timeout_seconds: float,
) -> EndpointResponse:
    target = url.rstrip("/") + "/" + path.lstrip("/") if path else url
    request_headers = dict(headers or {})
    if token and not any(key.lower() == "authorization" for key in request_headers):
        request_headers["Authorization"] = f"Bearer {token}"
    query: list[tuple[str, str]] | None = None
    if params is not None:
        items: Iterable[tuple[str, object]] = (
            cast("Mapping[str, object]", params).items() if isinstance(params, Mapping) else params
        )
        query = [(str(key), str(value)) for key, value in items]
        target += ("&" if "?" in target else "?") + urlencode(query)
    try:
        response = httpx.request(
            method.upper(),
            target,
            json=json_body if data is None else None,
            content=data,
            headers=request_headers,
            timeout=httpx.Timeout(30.0, read=timeout_seconds),
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        from lazycloud.abstractions.endpoint import EndpointOperationError

        raise EndpointOperationError(f"{method.upper()} {target}: {exc}") from exc
    grouped: dict[str, list[str]] = {}
    for key, value in response.headers.multi_items():
        grouped.setdefault(key, []).append(value)
    return EndpointResponse(
        status_code=response.status_code,
        headers=grouped,
        content=response.content,
        url=str(response.url),
    )


__all__ = [
    "EndpointResponse",
    "InvocationOptions",
    "InvocationTargetError",
    "InvocationTargetName",
    "http_function_spec",
    "resolve_url",
    "send_request",
    "unsupported_http_options",
]

from __future__ import annotations

import inspect
from collections.abc import Iterable
from enum import Enum
from types import UnionType
from typing import Any, get_args, get_origin

from shared.app_identity import CLI_NAME

import lazycloud

SDK_OBJECTS: tuple[type[Any], ...] = (
    lazycloud.App,
    lazycloud.Image,
    lazycloud.Queue,
    lazycloud.Map,
    lazycloud.Secret,
    lazycloud.Volume,
    lazycloud.Container,
    lazycloud.Artifact,
    lazycloud.Client,
    lazycloud.Task,
    lazycloud.Deployment,
    lazycloud.GpuType,
    lazycloud.Pool,
    lazycloud.QueueDepthAutoscaler,
    lazycloud.RetryBackoff,
    lazycloud.RetryPolicy,
    lazycloud.TaskPolicy,
)

SDK_MODULES: tuple[str, ...] = (
    "lazycloud.env",
    "lazycloud.config",
    "lazycloud.exceptions",
    "lazycloud.aio",
    "lazycloud.middleware",
    "lazycloud.multipart",
    "lazycloud.schema",
    "lazycloud.abstractions.sandbox",
)


def render_sdk_reference(
    objects: Iterable[type[Any]] = SDK_OBJECTS,
    modules: Iterable[str] = SDK_MODULES,
) -> str:
    lines = [
        "# SDK Reference",
        "",
        "Generated from the current Python SDK surface.",
        "",
        "## Execution Behavior Notes",
        "",
        '- Define deployable resources through `app = App("slug")`, then use',
        "  `@app.function`, `@app.endpoint`, `@app.asgi`, and `@app.task_queue`.",
        "- Function objects call the remote execution service when invoked directly.",
        "  Use `.local(*args, **kwargs)` for in-process execution.",
        "- Endpoint and task queue objects follow the beta-style resource surface:",
        "  endpoints are invoked over HTTP and task queues enqueue with",
        "  `.put(...)`; `.local(...)` is reserved for explicit in-process execution.",
        "- `Image.from_dockerfile(...).add_local_path(...)` archives the selected local",
        "  build context automatically during SDK `prepare()` and `deploy()` workflows,",
        "  uploads it as a build-context object, builds the image, and preserves the",
        "  built image id plus image packages, build steps, env vars, Dockerfile,",
        "  context object, credentials keys, secrets, and GPU hint in the deployment",
        "  stub request.",
        "- Sandbox Docker helpers wait for Docker daemon readiness, support password-safe",
        "  registry login, default `docker run` and `docker build` to host networking for",
        "  gVisor-safe Docker-in-sandbox behavior, and can generate Compose override",
        "  files with host networking.",
        f"- Attach your own hardware with `{CLI_NAME} machine join`, list it with",
        f"  `{CLI_NAME} machine list`, and detach it with `{CLI_NAME} machine remove`.",
        "  `Client().compute.machine_join_command(...)` returns the same join command.",
        "",
        "## Public Modules",
        "",
    ]
    for module in modules:
        lines.append(f"- `{module}`")
    lines.extend(
        [
            "",
            "## Public Objects",
            "",
        ]
    )
    for obj in objects:
        lines.extend(_render_object(obj))
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    print(render_sdk_reference(), end="")


def _render_object(obj: type[Any]) -> list[str]:
    lines = [f"## `{obj.__name__}`", ""]
    doc = inspect.getdoc(obj)
    if doc and not doc.startswith(f"{obj.__name__}("):
        lines.extend([doc.splitlines()[0], ""])
    if issubclass(obj, Enum):
        lines.extend(_render_enum(obj))
    else:
        lines.extend(_render_methods(obj))
    lines.append("")
    return lines


def _render_enum(obj: type[Enum]) -> list[str]:
    lines = ["| Name | Value |", "| --- | --- |"]
    for item in obj:
        lines.append(f"| `{item.name}` | `{item.value}` |")
    return lines


def _render_methods(obj: type[Any]) -> list[str]:
    methods: list[str] = []
    for name, member in _declared_callables(obj):
        if name.startswith("_"):
            continue
        try:
            signature = inspect.signature(member)
        except (TypeError, ValueError):
            continue
        methods.extend([f"### `{obj.__name__}.{name}{signature}`", ""])
        method_doc = inspect.getdoc(member)
        if method_doc:
            methods.extend([method_doc.splitlines()[0], ""])
        parameters = [
            parameter for parameter in signature.parameters.values() if parameter.name != "self"
        ]
        if parameters:
            methods.extend(["| Parameter | Required | Annotation |", "| --- | --- | --- |"])
            for parameter in parameters:
                required = parameter.default is inspect.Signature.empty
                methods.append(
                    f"| `{parameter.name}` | `{str(required).lower()}` | "
                    f"`{_format_annotation(parameter.annotation)}` |"
                )
            methods.append("")
        if signature.return_annotation is not inspect.Signature.empty:
            methods.extend([f"Returns: `{_format_annotation(signature.return_annotation)}`", ""])
    return methods or ["No public methods.", ""]


def _declared_callables(obj: type[Any]) -> list[tuple[str, Any]]:
    members: list[tuple[str, Any]] = []
    for name, raw_member in sorted(obj.__dict__.items()):
        member: Any = getattr(raw_member, "__func__", raw_member)
        if callable(member):
            members.append((name, member))
    return members


def _format_annotation(annotation: Any) -> str:
    if annotation is inspect.Signature.empty:
        return ""
    if isinstance(annotation, str):
        return annotation
    if annotation is None:
        return "None"
    origin = get_origin(annotation)
    if origin is not None:
        args = ", ".join(_format_annotation(arg) for arg in get_args(annotation))
        name = getattr(origin, "__name__", str(origin))
        return f"{name}[{args}]"
    if isinstance(annotation, UnionType):
        return " | ".join(_format_annotation(arg) for arg in get_args(annotation))
    return getattr(annotation, "__name__", str(annotation)).replace("typing.", "")


if __name__ == "__main__":
    main()

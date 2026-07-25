from __future__ import annotations

import hashlib
import json
import keyword
import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from shared.app_slug import validate_app_slug
from shared.deployments import DeploymentKind
from shared.http.client_manifests import (
    CLIENT_MANIFEST_DEPLOYMENT_KINDS,
    ClientContract,
    ClientManifestRequest,
    ClientManifestResource,
    ClientParameter,
)
from shared.http.errors import HttpApiError

from lazycloud.clients.gateway.control import GatewayControlClient
from lazycloud.control import resolve_control_client_config
from lazycloud.exceptions import ClientGenerationError
from lazycloud.json_contracts import JsonValue, parse_json_object, validate_json_object

CLIENT_PACKAGE_ROOT = Path("lazycloud_clients")


def remove_client_package(*, app: str, output: Path) -> dict[str, JsonValue]:
    """Delete a generated client package and drop it from the lock file."""
    slug = validate_app_slug(app)
    target = output / slug
    if target.exists():
        shutil.rmtree(target)
    lock = _read_lock(output)
    lock.pop(slug, None)
    _write_lock(output, lock)
    _write_root_package(output, lock)
    return {"app": slug, "removed": True, "output": str(target)}


def write_client_package(
    *,
    app: str,
    workspace: str,
    output: Path,
) -> dict[str, JsonValue]:
    """Fetch an app client manifest and write its versioned typed client package."""
    slug = validate_app_slug(app)
    config = resolve_control_client_config(workspace=workspace)
    try:
        response = GatewayControlClient.from_endpoint(
            config.endpoint,
            token=config.token,
            timeout_seconds=config.timeout_seconds,
        ).client_manifest(
            ClientManifestRequest(
                app=slug,
                workspace=config.workspace,
                external_url=config.endpoint,
            )
        )
    except HttpApiError as exc:
        raise ClientGenerationError(
            exc.detail or f"failed to fetch client manifest for {slug}"
        ) from exc

    resources = [
        item for item in response.resources if item.kind in CLIENT_MANIFEST_DEPLOYMENT_KINDS
    ]
    _validate_typed_resources(slug, resources)
    manifest_payload = [validate_json_object(item.model_dump(mode="json")) for item in resources]
    version = _manifest_version(manifest_payload)
    package_root = output / slug
    version_root = package_root / f"v_{version}"
    output.mkdir(parents=True, exist_ok=True)
    version_root.mkdir(parents=True, exist_ok=True)

    _write_version_package(version_root, resources)
    _write_app_package(package_root, version=version)
    lock = _read_lock(output)
    lock[slug] = validate_json_object(
        {
            "app": slug,
            "workspace": response.workspace,
            "version": version,
            "resources": manifest_payload,
        }
    )
    _write_lock(output, lock)
    _write_root_package(output, lock)
    return {
        "app": slug,
        "workspace": response.workspace,
        "version": version,
        "package": f"{output.name}.{slug}",
        "path": str(package_root),
        "resources": [_resource_symbol(item) for item in resources],
    }


def _write_version_package(path: Path, resources: list[ClientManifestResource]) -> None:
    symbols = _resource_symbols(resources)
    manifest_json = json.dumps(
        [item.model_dump(mode="json") for item in resources],
        indent=4,
        sort_keys=True,
    )
    lines = [
        "from __future__ import annotations",
        "",
        "from collections.abc import AsyncIterator as _AsyncIterator",
        "from collections.abc import Iterable as _Iterable",
        "from collections.abc import Iterator as _Iterator",
        "from json import loads as _json_loads",
        "from typing import Any as _Any",
        "from typing import Literal as _Literal",
        "",
        "from pydantic import BaseModel as _BaseModel",
        "from pydantic import Field as _Field",
        "from pydantic import TypeAdapter as _TypeAdapter",
        "from lazycloud.abstractions.endpoint import EndpointResponse as _EndpointResponse",
        "from lazycloud.client_handles import (",
        "    ASGIHandle as _ASGIHandle,",
        "    EndpointHandle as _EndpointHandle,",
        "    TaskQueueHandle as _TaskQueueHandle,",
        "    handle_from_manifest as _handle_from_manifest,",
        ")",
        "from lazycloud.session.task import Task as _Task",
        "from lazycloud.session.task import TaskBatch as _TaskBatch",
        "from lazycloud.session.task import TaskResult as _TaskResult",
        "from lazycloud.session.task import TaskSubscription as _TaskSubscription",
        "from shared.http.observability import LogRecord as _LogRecord",
        "from shared.http.tasks import TaskResponse as _TaskResponse",
        "from shared.http.tasks import TaskStopResponse as _TaskStopResponse",
        "from shared.tasks import Task as _TaskRecord",
        "",
        f"_MANIFEST = _json_loads({manifest_json!r})",
        "",
    ]
    for index, resource in enumerate(resources):
        symbol = symbols[index]
        lines.extend(_resource_wrapper_lines(resource, symbol=symbol, index=index))
    exported = [*symbols]
    lines.extend(["", f"__all__ = {json.dumps(exported, indent=4)}", ""])
    (path / "__init__.py").write_text("\n".join(lines), encoding="utf-8")
    (path / "py.typed").write_text("", encoding="utf-8")


def _write_app_package(path: Path, *, version: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "__init__.py").write_text(
        f"from .v_{version} import *\nfrom .v_{version} import __all__ as __all__\n",
        encoding="utf-8",
    )
    (path / "py.typed").write_text("", encoding="utf-8")


def _write_root_package(output: Path, lock: dict[str, JsonValue]) -> None:
    apps: list[str] = []
    for slug in sorted(lock):
        try:
            app = validate_app_slug(slug)
        except ValueError:
            continue
        if (output / app / "__init__.py").is_file():
            apps.append(app)
    lines = ["from __future__ import annotations", ""]
    lines.extend(f"from . import {app} as {app}" for app in apps)
    lines.extend(["", f"__all__ = {json.dumps(apps, indent=4)}", ""])
    (output / "__init__.py").write_text("\n".join(lines), encoding="utf-8")
    (output / "py.typed").write_text("", encoding="utf-8")


def _manifest_version(resources: list[dict[str, JsonValue]]) -> str:
    raw = json.dumps(resources, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _validate_typed_resources(app: str, resources: list[ClientManifestResource]) -> None:
    stale = [resource for resource in resources if resource.client_contract is None]
    if not stale:
        return
    details = ", ".join(
        f"{resource.kind.value}:{resource.name}@v{resource.deployment_version}"
        for resource in stale
    )
    raise ClientGenerationError(
        f"client manifest for app {app!r} has callable deployments without typed "
        f"client shared: {details}. Redeploy these resources with the current SDK "
        "and then run `lazycloud client get` again."
    )


def _resource_symbols(resources: list[ClientManifestResource]) -> list[str]:
    used: set[str] = set()
    symbols: list[str] = []
    for resource in resources:
        base = _python_name(resource.name)
        selected = base
        if selected in used:
            selected = f"{base}_{resource.kind.value.replace('-', '_')}"
        index = 2
        while selected in used:
            selected = f"{base}_{index}"
            index += 1
        used.add(selected)
        symbols.append(selected)
    return symbols


def _resource_symbol(resource: ClientManifestResource) -> dict[str, JsonValue]:
    return {
        "name": resource.name,
        "kind": resource.kind.value,
        "deployment_version": resource.deployment_version,
        "invoke_url": resource.invoke_url,
    }


def _resource_wrapper_lines(
    resource: ClientManifestResource,
    *,
    symbol: str,
    index: int,
) -> list[str]:
    class_name = _private_class_name(symbol, resource.kind)
    lines: list[str] = []
    contract = resource.client_contract
    if contract is None:
        raise ValueError(
            f"{resource.kind.value}:{resource.name}@v{resource.deployment_version} "
            "is missing a client contract"
        )
    schema_context = _contract_schema_context(contract, symbol=symbol)
    lines.extend(_contract_model_lines(schema_context))
    if resource.kind is DeploymentKind.TaskQueue and contract.operation.return_schema:
        if lines:
            lines.append("")
        lines.extend(
            _task_wrapper_lines(
                symbol,
                _annotation_from_json_schema(
                    contract.operation.return_schema,
                    schema_context,
                ),
            )
        )
    if lines:
        lines.append("")
    lines.extend(
        [
            f"class {class_name}:",
            '    __slots__ = ("_handle",)',
            *[
                f"    {public_name} = {private_name}"
                for public_name, private_name in schema_context.public_aliases
            ],
            "",
            "    def __init__(self) -> None:",
            f"        handle = _handle_from_manifest(_MANIFEST[{index}])",
            f"        if not isinstance(handle, {_handle_type(resource.kind)}):",
            "            raise TypeError('client manifest handle kind mismatch')",
            "        self._handle = handle",
            "",
        ]
    )
    lines.extend(
        _contract_operation_lines(
            resource,
            contract=contract,
            context=schema_context,
            symbol=symbol,
        )
    )
    lines.extend(["", f"{symbol} = {class_name}()", ""])
    return lines


@dataclass(frozen=True, slots=True)
class _GeneratedSchemaModel:
    class_name: str
    schema: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _SchemaContext:
    models: list[_GeneratedSchemaModel]
    ref_names: dict[str, str]
    schema_names: dict[int, str]
    public_aliases: list[tuple[str, str]]


def _contract_schema_context(
    contract: ClientContract,
    *,
    symbol: str,
) -> _SchemaContext:
    used: set[str] = set()
    prefix = _python_class_name(symbol)
    models: list[_GeneratedSchemaModel] = []
    ref_names: dict[str, str] = {}
    schema_names: dict[int, str] = {}
    public_aliases: list[tuple[str, str]] = []
    source_schemas = [parameter.json_schema for parameter in contract.operation.parameters]
    source_schemas.append(contract.operation.return_schema)
    for source_schema in source_schemas:
        schema = validate_json_object(source_schema)
        for key, definition in _schema_defs(schema).items():
            if not _schema_is_model(definition):
                continue
            class_name = _unique_private_model_name(prefix, definition, key, used)
            ref_names[f"#/$defs/{key}"] = class_name
            public_aliases.append((_public_model_name(definition, key), class_name))
            models.append(_GeneratedSchemaModel(class_name=class_name, schema=definition))
    for parameter in contract.operation.parameters:
        schema = validate_json_object(parameter.json_schema)
        class_name = _schema_model_name(
            schema,
            prefix=prefix,
            fallback=_python_class_name(parameter.name),
            used=used,
        )
        if class_name is None:
            continue
        schema_names[id(parameter.json_schema)] = class_name
        public_aliases.append(
            (
                _public_model_name(schema, parameter.name),
                class_name,
            )
        )
        models.append(_GeneratedSchemaModel(class_name=class_name, schema=schema))
    return_schema = validate_json_object(contract.operation.return_schema)
    return_class_name = _schema_model_name(
        return_schema,
        prefix=prefix,
        fallback="Output",
        used=used,
    )
    if return_class_name is not None:
        schema_names[id(contract.operation.return_schema)] = return_class_name
        public_aliases.append(
            (
                _public_model_name(return_schema, "Output"),
                return_class_name,
            )
        )
        models.append(
            _GeneratedSchemaModel(
                class_name=return_class_name,
                schema=return_schema,
            )
        )
    return _SchemaContext(
        models=models,
        ref_names=ref_names,
        schema_names=schema_names,
        public_aliases=_unique_aliases(public_aliases),
    )


def _contract_model_lines(
    context: _SchemaContext,
) -> list[str]:
    lines: list[str] = []
    for model in context.models:
        properties = _schema_properties(model.schema)
        required = _schema_required(model.schema)
        lines.append(f"class {model.class_name}(_BaseModel):")
        if not properties:
            lines.append("    pass")
            lines.append("")
            continue
        for name, schema in properties.items():
            field_name = _python_name(name)
            annotation = _annotation_from_json_schema(schema, context)
            default = _model_field_default(name, schema, required=name in required)
            lines.append(f"    {field_name}: {annotation}{default}")
        lines.append("")
    for model in context.models:
        lines.append(f"{model.class_name}.model_rebuild()")
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _schema_defs(schema: Mapping[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    raw_defs = schema.get("$defs")
    if not isinstance(raw_defs, dict):
        return {}
    return {
        str(name): definition
        for name, definition in raw_defs.items()
        if isinstance(definition, dict)
    }


def _schema_is_model(schema: Mapping[str, JsonValue]) -> bool:
    return isinstance(schema.get("properties"), dict)


def _schema_model_name(
    schema: Mapping[str, JsonValue],
    *,
    prefix: str,
    fallback: str,
    used: set[str],
) -> str | None:
    if "$ref" in schema or not _schema_is_model(schema):
        return None
    return _unique_private_model_name(prefix, schema, fallback, used)


def _unique_private_model_name(
    prefix: str,
    schema: Mapping[str, JsonValue],
    fallback: str,
    used: set[str],
) -> str:
    base = f"_{prefix}{_python_class_name(str(schema.get('title') or fallback))}"
    selected = base
    index = 2
    while selected in used:
        selected = f"{base}{index}"
        index += 1
    used.add(selected)
    return selected


def _public_model_name(schema: Mapping[str, JsonValue], fallback: str) -> str:
    return _python_class_name(str(schema.get("title") or fallback))


def _unique_aliases(aliases: list[tuple[str, str]]) -> list[tuple[str, str]]:
    used: set[str] = set()
    selected: list[tuple[str, str]] = []
    for public_name, private_name in aliases:
        candidate = public_name
        index = 2
        while candidate in used:
            candidate = f"{public_name}{index}"
            index += 1
        used.add(candidate)
        selected.append((candidate, private_name))
    return selected


def _schema_properties(
    schema: Mapping[str, JsonValue],
) -> dict[str, dict[str, JsonValue]]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(name): value for name, value in properties.items() if isinstance(value, dict)}


def _schema_required(schema: Mapping[str, JsonValue]) -> set[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return set()
    return {str(item) for item in required}


def _model_field_default(
    name: str,
    schema: Mapping[str, JsonValue],
    *,
    required: bool,
) -> str:
    field_name = _python_name(name)
    alias = "" if field_name == name else f", alias={name!r}"
    if "default" in schema:
        return f" = _Field({schema['default']!r}{alias})"
    if not required:
        return f" = _Field(None{alias})" if alias else " = None"
    return f" = _Field({alias.lstrip(', ')})" if alias else ""


def _task_wrapper_lines(symbol: str, value_annotation: str) -> list[str]:
    task_name = _private_task_name(symbol)
    batch_name = _private_task_batch_name(symbol)
    result_name = _private_task_result_name(symbol)
    return [
        f"class {result_name}:",
        '    __slots__ = ("_result", "value")',
        "",
        "    def __init__(self, result: _TaskResult) -> None:",
        "        self._result = result",
        f"        self.value: {value_annotation} = _TypeAdapter(",
        f"            {value_annotation}",
        "        ).validate_python(result.value)",
        "",
        "    @property",
        "    def id(self) -> str:",
        "        return self._result.id",
        "",
        "    @property",
        "    def ok(self) -> bool:",
        "        return self._result.ok",
        "",
        "    @property",
        "    def error(self) -> str:",
        "        return self._result.error",
        "",
        "    @property",
        "    def exit_code(self) -> int | None:",
        "        return self._result.exit_code",
        "",
        f"class {task_name}:",
        '    __slots__ = ("_task",)',
        "",
        "    def __init__(self, task: _Task) -> None:",
        "        self._task = task",
        "",
        "    @property",
        "    def task_id(self) -> str:",
        "        return self._task.task_id",
        "",
        "    def result(",
        "        self,",
        "        *,",
        "        wait: bool = False,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> {result_name}:",
        "        result = self._task.result(",
        "            wait=wait,",
        "            timeout_seconds=timeout_seconds,",
        "            poll_interval_seconds=poll_interval_seconds,",
        "        )",
        f"        return {result_name}(result)",
        "",
        "    def wait(",
        "        self,",
        "        *,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> {result_name}:",
        "        result = self._task.wait(",
        "            timeout_seconds=timeout_seconds,",
        "            poll_interval_seconds=poll_interval_seconds,",
        "        )",
        f"        return {result_name}(result)",
        "",
        "    async def async_wait(",
        "        self,",
        "        *,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> {result_name}:",
        "        result = await self._task.async_wait(",
        "            timeout_seconds=timeout_seconds,",
        "            poll_interval_seconds=poll_interval_seconds,",
        "        )",
        f"        return {result_name}(result)",
        "",
        "    def get(self) -> _TaskRecord:",
        "        return self._task.get()",
        "",
        "    def view(self) -> _TaskResponse:",
        "        return self._task.view()",
        "",
        "    def subscribe(self) -> _TaskSubscription:",
        "        return self._task.subscribe()",
        "",
        "    def logs(self, *, limit: int = 100, page: int = 0) -> list[_LogRecord]:",
        "        return self._task.logs(limit=limit, page=page)",
        "",
        "    def output(self, *, limit: int = 100, page: int = 0) -> str:",
        "        return self._task.output(limit=limit, page=page)",
        "",
        "    def cancel(self) -> _TaskStopResponse:",
        "        return self._task.cancel()",
        "",
        f"class {batch_name}:",
        '    __slots__ = ("_batch",)',
        "",
        "    def __init__(self, batch: _TaskBatch) -> None:",
        "        self._batch = batch",
        "",
        f"    def __iter__(self) -> _Iterator[{task_name}]:",
        "        for task in self._batch:",
        f"            yield {task_name}(task)",
        "",
        "    def __len__(self) -> int:",
        "        return len(self._batch)",
        "",
        "    def wait(",
        "        self,",
        "        *,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> list[{result_name}]:",
        "        return [",
        f"            {result_name}(result)",
        "            for result in self._batch.wait(",
        "                timeout_seconds=timeout_seconds,",
        "                poll_interval_seconds=poll_interval_seconds,",
        "            )",
        "        ]",
        "",
        "    async def async_wait(",
        "        self,",
        "        *,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> list[{result_name}]:",
        "        return [",
        f"            {result_name}(result)",
        "            for result in await self._batch.async_wait(",
        "                timeout_seconds=timeout_seconds,",
        "                poll_interval_seconds=poll_interval_seconds,",
        "            )",
        "        ]",
        "",
        "    def as_completed(",
        "        self,",
        "        *,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> _Iterator[{result_name}]:",
        "        for result in self._batch.as_completed(",
        "            timeout_seconds=timeout_seconds,",
        "            poll_interval_seconds=poll_interval_seconds,",
        "        ):",
        f"            yield {result_name}(result)",
        "",
        "    async def async_as_completed(",
        "        self,",
        "        *,",
        "        timeout_seconds: float | None = None,",
        "        poll_interval_seconds: float = 1.0,",
        f"    ) -> _AsyncIterator[{result_name}]:",
        "        async for result in self._batch.async_as_completed(",
        "            timeout_seconds=timeout_seconds,",
        "            poll_interval_seconds=poll_interval_seconds,",
        "        ):",
        f"            yield {result_name}(result)",
    ]


def _contract_operation_lines(
    resource: ClientManifestResource,
    *,
    contract: ClientContract,
    context: _SchemaContext,
    symbol: str,
) -> list[str]:
    operation = contract.operation
    method_name = operation.name.value
    parameters = operation.parameters
    if any(item.parameter_kind in {"var_positional", "var_keyword"} for item in parameters):
        return _contract_variadic_operation_lines(resource, method_name)
    if resource.kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return _endpoint_operation_lines(resource, contract=contract, context=context)
    if resource.kind is DeploymentKind.TaskQueue:
        return _task_queue_operation_lines(
            resource,
            contract=contract,
            context=context,
            symbol=symbol,
        )
    signature = _contract_signature(parameters, context)
    return_annotation = _contract_return_annotation(resource, contract, context)
    call = _contract_call(resource, method_name, parameters)
    lines = [
        f"    def {method_name}(self{signature}) -> {return_annotation}:",
        f"        return {call}",
    ]
    return lines


def _endpoint_operation_lines(
    resource: ClientManifestResource,
    *,
    contract: ClientContract,
    context: _SchemaContext,
) -> list[str]:
    method_name = contract.operation.name.value
    parameters = contract.operation.parameters
    signature = _contract_signature(parameters, context)
    return_annotation = _contract_return_annotation(resource, contract, context)
    sync_call = _contract_call(resource, method_name, parameters)
    async_call = _contract_call(
        resource,
        method_name,
        parameters,
        handle_method=f"async_{method_name}",
    )
    return [
        f"    def {method_name}(self{signature}) -> {return_annotation}:",
        *_return_endpoint_call_lines(sync_call, return_annotation, await_call=False),
        "",
        f"    async def async_{method_name}(self{signature}) -> {return_annotation}:",
        *_return_endpoint_call_lines(async_call, return_annotation, await_call=True),
    ]


def _return_endpoint_call_lines(
    call: str,
    return_annotation: str,
    *,
    await_call: bool,
) -> list[str]:
    if return_annotation == "_EndpointResponse":
        return [f"        return {'await ' if await_call else ''}{call}"]
    response = f"(await {call})" if await_call else call
    return [
        f"        return _TypeAdapter({return_annotation}).validate_python(",
        f"            {response}.json()",
        "        )",
    ]


def _task_queue_operation_lines(
    resource: ClientManifestResource,
    *,
    contract: ClientContract,
    context: _SchemaContext,
    symbol: str,
) -> list[str]:
    method_name = contract.operation.name.value
    parameters = contract.operation.parameters
    signature = _contract_signature(parameters, context)
    sync_call = _contract_call(resource, method_name, parameters)
    async_call = _contract_call(
        resource,
        method_name,
        parameters,
        handle_method=f"async_{method_name}",
    )
    if contract.operation.return_schema:
        task_name = _private_task_name(symbol)
        batch_name = _private_task_batch_name(symbol)
        return [
            f"    def {method_name}(self{signature}) -> {task_name} | bool:",
            f"        submitted = {sync_call}",
            *_typed_task_return_lines(task_name),
            "",
            f"    async def async_{method_name}(self{signature}) -> {task_name} | bool:",
            f"        submitted = await {async_call}",
            *_typed_task_return_lines(task_name),
            "",
            *_task_queue_put_many_lines(
                parameters,
                batch_annotation=batch_name,
                context=context,
                wrap_batch=True,
            ),
        ]
    return [
        f"    def {method_name}(self{signature}) -> _Task | bool:",
        f"        return {sync_call}",
        "",
        f"    async def async_{method_name}(self{signature}) -> _Task | bool:",
        f"        return await {async_call}",
        "",
        *_task_queue_put_many_lines(
            parameters,
            batch_annotation="_TaskBatch",
            context=context,
            wrap_batch=False,
        ),
    ]


def _typed_task_return_lines(task_name: str) -> list[str]:
    return [
        "        return (",
        f"            {task_name}(submitted)",
        "            if isinstance(submitted, _Task)",
        "            else False",
        "        )",
    ]


def _task_queue_put_many_lines(
    parameters: list[ClientParameter],
    *,
    batch_annotation: str,
    context: _SchemaContext,
    wrap_batch: bool,
) -> list[str]:
    signature = _task_queue_put_many_signature(parameters, context)
    sync_call = _task_queue_put_many_call(parameters, handle_method="put_many")
    async_call = _task_queue_put_many_call(parameters, handle_method="async_put_many")
    sync_return = f"{batch_annotation}({sync_call})" if wrap_batch else sync_call
    async_return = (
        f"{batch_annotation}(await {async_call})" if wrap_batch else f"await {async_call}"
    )
    return [
        f"    def put_many(self{signature}) -> {batch_annotation}:",
        f"        return {sync_return}",
        "",
        f"    async def async_put_many(self{signature}) -> {batch_annotation}:",
        f"        return {async_return}",
    ]


def _task_queue_put_many_signature(
    parameters: list[ClientParameter],
    context: _SchemaContext,
) -> str:
    if not parameters:
        return ", items: _Iterable[_Any]"
    first = parameters[0]
    item_annotation = _annotation_from_json_schema(first.json_schema, context)
    shared = [
        (
            f"{_python_name(parameter.name)}: "
            f"{_annotation_from_json_schema(parameter.json_schema, context)}"
            f"{_default_suffix(parameter.required, parameter.default_repr)}"
        )
        for parameter in parameters[1:]
    ]
    signature = f", items: _Iterable[{item_annotation}]"
    if shared:
        signature += ", *, " + ", ".join(shared)
    return signature


def _task_queue_put_many_call(
    parameters: list[ClientParameter],
    *,
    handle_method: str,
) -> str:
    if len(parameters) <= 1:
        return f"self._handle.{handle_method}(items)"
    shared_items = ", ".join(
        f"{parameter.name!r}: {_python_name(parameter.name)}" for parameter in parameters[1:]
    )
    return f"self._handle.{handle_method}(items, **{{{shared_items}}})"


def _contract_variadic_operation_lines(
    resource: ClientManifestResource,
    method_name: str,
) -> list[str]:
    return_annotation = _kind_return_annotation(resource.kind)
    lines = [
        f"    def {method_name}(self, *args: _Any, **kwargs: _Any) -> {return_annotation}:",
        f"        return self._handle.{method_name}(*args, **kwargs)",
    ]
    if resource.kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi, DeploymentKind.TaskQueue}:
        lines.extend(
            [
                "",
                (
                    f"    async def async_{method_name}"
                    f"(self, *args: _Any, **kwargs: _Any) -> {return_annotation}:"
                ),
                f"        return await self._handle.async_{method_name}(*args, **kwargs)",
            ]
        )
    return lines


def _contract_signature(
    parameters: list[ClientParameter],
    context: _SchemaContext,
) -> str:
    if not parameters:
        return ""
    rendered = [
        (
            f"{_python_name(parameter.name)}: "
            f"{_annotation_from_json_schema(parameter.json_schema, context)}"
            f"{_default_suffix(parameter.required, parameter.default_repr)}"
        )
        for parameter in parameters
    ]
    return ", *, " + ", ".join(rendered)


def _contract_call(
    resource: ClientManifestResource,
    method_name: str,
    parameters: list[ClientParameter],
    *,
    handle_method: str | None = None,
) -> str:
    selected_handle_method = handle_method or method_name
    if resource.kind is DeploymentKind.Asgi:
        if not parameters:
            return f"self._handle.{selected_handle_method}()"
        call_arguments = ", ".join(
            f"{parameter.name}={_python_name(parameter.name)}" for parameter in parameters
        )
        return f"self._handle.{selected_handle_method}({call_arguments})"
    if not parameters:
        return f"self._handle.{selected_handle_method}()"
    payload_items = ", ".join(
        f"{parameter.name!r}: {_python_name(parameter.name)}" for parameter in parameters
    )
    return f"self._handle.{selected_handle_method}(**{{{payload_items}}})"


def _contract_return_annotation(
    resource: ClientManifestResource,
    contract: ClientContract,
    context: _SchemaContext,
) -> str:
    if resource.kind is DeploymentKind.Asgi:
        return "_EndpointResponse"
    if resource.kind is DeploymentKind.Endpoint:
        if not contract.operation.return_schema:
            return "_EndpointResponse"
        return _annotation_from_json_schema(contract.operation.return_schema, context)
    if resource.kind is DeploymentKind.TaskQueue:
        return "_Task | bool"
    return "_Any"


def _kind_return_annotation(kind: DeploymentKind) -> str:
    if kind in {DeploymentKind.Endpoint, DeploymentKind.Asgi}:
        return "_EndpointResponse"
    if kind is DeploymentKind.TaskQueue:
        return "_Task | bool"
    return "_Any"


def _annotation_from_json_schema(
    schema: JsonValue,
    context: _SchemaContext,
) -> str:
    schema_identity = id(schema)
    schema = validate_json_object(schema)
    if not schema:
        return "_Any"
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return context.ref_names.get(ref, "_Any")
    if schema_identity in context.schema_names:
        return context.schema_names[schema_identity]
    if "const" in schema:
        return f"_Literal[{schema['const']!r}]"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return f"_Literal[{', '.join(repr(item) for item in enum)}]"
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list):
            rendered = _unique_annotations(
                _annotation_from_json_schema(item, context)
                for item in options
                if isinstance(item, dict)
            )
            return " | ".join(rendered) if rendered else "_Any"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        rendered = _unique_annotations(
            _annotation_from_json_schema({**schema, "type": item}, context)
            for item in schema_type
            if isinstance(item, str)
        )
        return " | ".join(rendered) if rendered else "_Any"
    if schema_type == "null":
        return "None"
    if schema_type == "string":
        return "bytes" if schema.get("format") == "binary" else "str"
    if schema_type == "integer":
        return "int"
    if schema_type == "number":
        return "float"
    if schema_type == "boolean":
        return "bool"
    if schema_type == "array":
        prefix_items = schema.get("prefixItems")
        if isinstance(prefix_items, list) and prefix_items:
            items = [
                _annotation_from_json_schema(item, context)
                for item in prefix_items
                if isinstance(item, dict)
            ]
            return f"tuple[{', '.join(items)}]" if items else "tuple[()]"
        item_schema = schema.get("items")
        item = (
            _annotation_from_json_schema(item_schema, context)
            if isinstance(item_schema, dict)
            else "_Any"
        )
        return f"set[{item}]" if schema.get("uniqueItems") is True else f"list[{item}]"
    if schema_type == "object" or "properties" in schema:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"dict[str, {_annotation_from_json_schema(additional, context)}]"
        return "dict[str, _Any]"
    return "_Any"


def _unique_annotations(values: Iterable[str]) -> list[str]:
    rendered: list[str] = []
    for value in values:
        if isinstance(value, str) and value not in rendered:
            rendered.append(value)
    return rendered


def _default_suffix(required: bool, default_repr: str) -> str:
    if required:
        return ""
    return f" = {default_repr or 'None'}"


def _private_class_name(symbol: str, kind: DeploymentKind) -> str:
    return f"_{_python_class_name(symbol)}{_python_class_name(kind.value)}"


def _private_task_name(symbol: str) -> str:
    return f"_{_python_class_name(symbol)}Task"


def _private_task_batch_name(symbol: str) -> str:
    return f"_{_python_class_name(symbol)}TaskBatch"


def _private_task_result_name(symbol: str) -> str:
    return f"_{_python_class_name(symbol)}TaskResult"


def _python_class_name(value: str) -> str:
    words = re.split(r"[^0-9A-Za-z]+|_", value)
    name = "".join(word[:1].upper() + word[1:] for word in words if word)
    if not name or name[0].isdigit():
        name = f"Resource{name}"
    return name


def _python_name(value: str) -> str:
    normalized = re.sub(r"\W+", "_", value.strip()).strip("_").lower()
    if not normalized or normalized[0].isdigit() or keyword.iskeyword(normalized):
        normalized = f"resource_{normalized or 'handle'}"
    return normalized


def _handle_type(kind: DeploymentKind) -> str:
    if kind is DeploymentKind.Endpoint:
        return "_EndpointHandle"
    if kind is DeploymentKind.Asgi:
        return "_ASGIHandle"
    if kind is DeploymentKind.TaskQueue:
        return "_TaskQueueHandle"
    raise ValueError(f"unsupported typed client resource kind: {kind.value}")


def _read_lock(output: Path) -> dict[str, JsonValue]:
    path = output / "lazycloud-clients.lock.json"
    if not path.exists():
        return {}
    try:
        return parse_json_object(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _write_lock(output: Path, lock: dict[str, JsonValue]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "lazycloud-clients.lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CLIENT_PACKAGE_ROOT",
    "remove_client_package",
    "write_client_package",
]

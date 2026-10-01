"""Typed client packages for deployed apps, written by `lazycloud app export`.

A package is `<output>/<app>/v_<version>` plus an `<app>` package that
re-exports its current version. The version hashes every exported manifest,
which carries the function contracts and active release ids, so a redeploy
that changes either needs a new export.
"""

from __future__ import annotations

import hashlib
import json
import keyword
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from pydantic import ValidationError
from shared.api import WorkloadState
from shared.app_slug import validate_app_slug
from shared.http.client_manifests import ClientContract, ClientParameter

from lazycloud.client_handles import ResourceKind, ResourceManifest
from lazycloud.clients.api import ApiClient
from lazycloud.control import api_client, require_workspace, resolve_control_client_config
from lazycloud.exceptions import SdkError
from lazycloud.json_contracts import JsonValue, parse_json_object, validate_json_object

CLIENT_PACKAGE_ROOT = Path("lazycloud_clients")
_LOCK_FILE = "lazycloud-clients.lock.json"
_PAGE_LIMIT = 1000


class ClientGenerationError(SdkError):
    pass


class ExportedResource(TypedDict):
    name: str
    kind: str
    deployment_version: int
    release_id: str


class ClientPackageExport(TypedDict):
    app: str
    workspace: str
    version: str
    package: str
    path: str
    resources: list[ExportedResource]
    asgi_without_schema: list[str]


def write_client_package(
    *,
    app: str,
    workspace: str | None,
    output: Path,
    openapi_files: Mapping[str, Path] | None = None,
    openapi_paths: Mapping[str, str] | None = None,
) -> ClientPackageExport:
    """Read an app's deployed functions and write their versioned typed client package."""
    slug = validate_app_slug(app)
    if not output.name.isidentifier() or keyword.iskeyword(output.name):
        raise ClientGenerationError("output directory name must be a Python package identifier")
    lock = _read_lock(output)
    config = resolve_control_client_config(workspace=workspace)
    selected_workspace = require_workspace(config)
    with api_client(config) as client:
        resources = _deployed_functions(client, selected_workspace, slug)
    # Only ASGI resources take OpenAPI schemas, and apps deploy none yet.
    unknown = set(openapi_files or {}) | set(openapi_paths or {})
    if unknown:
        raise ClientGenerationError("unknown ASGI resources: " + ", ".join(sorted(unknown)))

    manifest_payload = [validate_json_object(item.model_dump(mode="json")) for item in resources]
    version = _manifest_version(manifest_payload)
    package_root = output / slug
    version_root = package_root / f"v_{version}"
    version_root.mkdir(parents=True, exist_ok=True)
    _write_version_package(
        version_root,
        resources,
        endpoint=config.endpoint,
        workspace=selected_workspace,
    )
    _write_app_package(package_root, version=version)
    lock[slug] = validate_json_object(
        {
            "app": slug,
            "workspace": selected_workspace,
            "version": version,
            "resources": manifest_payload,
        }
    )
    _write_lock(output, lock)
    _write_root_package(output, lock)
    return {
        "app": slug,
        "workspace": selected_workspace,
        "version": version,
        "package": f"{output.name}.{slug}",
        "path": str(package_root),
        "resources": [_exported_resource(item) for item in resources],
        "asgi_without_schema": [],
    }


def _deployed_functions(client: ApiClient, workspace: str, app: str) -> list[ResourceManifest]:
    """The app's functions with an active release, each with that release's contract."""
    resources: list[ResourceManifest] = []
    stale: list[str] = []
    cursor: str | None = None
    while True:
        page = client.list_deployments(workspace, app=app, limit=_PAGE_LIMIT, cursor=cursor)
        for workload in page.deployments:
            if workload.state is WorkloadState.deleted or workload.release_id is None:
                continue
            release = client.get_function(workspace, app, workload.name).active_release
            if release.version is None:
                msg = f"function {workload.name} has no deployed version"
                raise ClientGenerationError(msg)
            if release.spec.client_contract is None:
                stale.append(f"function:{workload.name}@v{release.version}")
                continue
            try:
                contract = ClientContract.model_validate(release.spec.client_contract)
            except ValidationError as exc:
                msg = f"function {workload.name} has an invalid client contract: {exc}"
                raise ClientGenerationError(msg) from exc
            resources.append(
                ResourceManifest(
                    app=app,
                    name=workload.name,
                    kind=ResourceKind.Function,
                    deployment_id=workload.id,
                    deployment_version=release.version,
                    release_id=release.id,
                    client_contract=contract,
                )
            )
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    if stale:
        raise ClientGenerationError(
            f"client manifest for app {app!r} has callable deployments without typed "
            f"contracts: {', '.join(stale)}. Redeploy these resources with the current SDK "
            "and then run `lazycloud app export` again."
        )
    if not resources:
        msg = f"app {app!r} has no deployed functions in workspace {workspace!r}"
        raise ClientGenerationError(msg)
    for resource in resources:
        _require_json_contract(resource.client_contract, resource.name)
    return sorted(resources, key=lambda item: item.name)


def _write_version_package(
    path: Path,
    resources: list[ResourceManifest],
    *,
    endpoint: str,
    workspace: str,
) -> None:
    symbols = _resource_symbols(resources)
    manifest_json = json.dumps(
        [item.model_dump(mode="json") for item in resources],
        indent=4,
        sort_keys=True,
    )
    lines = [
        "from __future__ import annotations",
        "",
        "from json import loads as _json_loads",
        "from typing import Literal as _Literal",
        "",
        "from pydantic import BaseModel as _BaseModel",
        "from pydantic import ConfigDict as _ConfigDict",
        "from pydantic import Field as _Field",
        "from pydantic import JsonValue as _JsonValue",
        "from pydantic import TypeAdapter as _TypeAdapter",
        "from lazycloud.client_handles import (",
        "    FunctionHandle as _FunctionHandle,",
        "    handle_from_manifest as _handle_from_manifest,",
        ")",
        "",
        f"_MANIFEST = _json_loads({manifest_json!r})",
        f"_ENDPOINT = {endpoint!r}",
        f"_WORKSPACE = {workspace!r}",
        "",
    ]
    for index, resource in enumerate(resources):
        lines.extend(_resource_wrapper_lines(resource, symbol=symbols[index], index=index))
    lines.extend(["", f"__all__ = {json.dumps(symbols, indent=4)}", ""])
    source = "\n".join(lines)
    compile(source, str(path / "__init__.py"), "exec")
    (path / "__init__.py").write_text(source, encoding="utf-8")
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


def _require_json_contract(contract: ClientContract, name: str) -> None:
    errors = contract.json_export_errors()
    if errors:
        raise ClientGenerationError(
            f"cannot export a JSON client for {name!r}: {'; '.join(errors)}. "
            "Call this function through its source Python SDK object."
        )


def _resource_symbols(resources: list[ResourceManifest]) -> list[str]:
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


def _exported_resource(resource: ResourceManifest) -> ExportedResource:
    return {
        "name": resource.name,
        "kind": resource.kind.value,
        "deployment_version": resource.deployment_version,
        "release_id": str(resource.release_id),
    }


def _resource_wrapper_lines(resource: ResourceManifest, *, symbol: str, index: int) -> list[str]:
    class_name = _private_class_name(symbol, resource.kind)
    contract = resource.client_contract
    schema_context = _contract_schema_context(contract, symbol=symbol)
    lines = _contract_model_lines(schema_context)
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
            "        handle = _handle_from_manifest(",
            f"            _MANIFEST[{index}], endpoint=_ENDPOINT, workspace=_WORKSPACE",
            "        )",
            f"        if not isinstance(handle, {_handle_type(resource.kind)}):",
            "            raise TypeError('client manifest handle kind mismatch')",
            "        self._handle = handle",
            "",
        ]
    )
    lines.extend(_contract_operation_lines(resource, contract=contract, context=schema_context))
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
    definitions: dict[str, dict[str, JsonValue]]
    schema_names: dict[str, str]
    public_aliases: list[tuple[str, str]]


def _contract_schema_context(contract: ClientContract, *, symbol: str) -> _SchemaContext:
    _require_json_contract(contract, symbol)
    used: set[str] = set()
    prefix = _python_class_name(symbol)
    models: list[_GeneratedSchemaModel] = []
    ref_names: dict[str, str] = {}
    definitions: dict[str, dict[str, JsonValue]] = {}
    schema_names: dict[str, str] = {}
    public_aliases: list[tuple[str, str]] = []
    source_schemas = [parameter.json_schema for parameter in contract.operation.parameters]
    source_schemas.append(contract.operation.return_schema)
    for source_schema in source_schemas:
        schema = validate_json_object(source_schema)
        for key, definition in _schema_defs(schema).items():
            definitions[f"#/$defs/{key}"] = definition
            if f"#/$defs/{key}" in ref_names:
                continue
            if not _schema_is_model(definition):
                continue
            class_name = _unique_private_model_name(prefix, definition, key, used)
            ref_names[f"#/$defs/{key}"] = class_name
            schema_names[_schema_key(definition)] = class_name
            public_aliases.append((_public_model_name(definition, key), class_name))
            models.append(_GeneratedSchemaModel(class_name=class_name, schema=definition))

    def register(schema: Mapping[str, JsonValue], fallback: str) -> None:
        key = _schema_key(schema)
        class_name = (
            _schema_model_name(schema, prefix=prefix, fallback=fallback, used=used)
            if key not in schema_names
            else None
        )
        if class_name is not None:
            schema_names[key] = class_name
            public_aliases.append((_public_model_name(schema, fallback), class_name))
            models.append(_GeneratedSchemaModel(class_name=class_name, schema=schema))
        for name, child in _schema_properties(schema).items():
            register(child, _python_class_name(name))
        for name in ("items", "additionalProperties"):
            child = schema.get(name)
            if isinstance(child, dict):
                register(child, fallback + "Item")
        for name in ("anyOf", "oneOf", "prefixItems"):
            children = schema.get(name)
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        register(child, fallback)

    for definition in definitions.values():
        register(definition, "Model")
    for parameter in contract.operation.parameters:
        register(validate_json_object(parameter.json_schema), _python_class_name(parameter.name))
    register(validate_json_object(contract.operation.return_schema), "Output")
    return _SchemaContext(
        models=models,
        ref_names=ref_names,
        definitions=definitions,
        schema_names=schema_names,
        public_aliases=_unique_aliases(public_aliases),
    )


def _schema_key(schema: Mapping[str, JsonValue]) -> str:
    return json.dumps(
        {key: value for key, value in schema.items() if key != "$defs"}, sort_keys=True
    )


def _contract_model_lines(context: _SchemaContext) -> list[str]:
    lines: list[str] = []
    for model in context.models:
        properties = _schema_properties(model.schema)
        required = _schema_required(model.schema)
        lines.append(f"class {model.class_name}(_BaseModel):")
        extra = "forbid" if model.schema.get("additionalProperties") is False else "allow"
        lines.append(f"    model_config = _ConfigDict(extra={extra!r})")
        if not properties:
            lines.append("    pass")
            lines.append("")
            continue
        for name, schema in properties.items():
            field_name = _python_name(name)
            annotation = _annotation_from_json_schema(schema, context)
            if name not in required and "default" not in schema and "None" not in annotation:
                annotation += " | None"
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


def _schema_properties(schema: Mapping[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {str(name): value for name, value in properties.items() if isinstance(value, dict)}


def _schema_required(schema: Mapping[str, JsonValue]) -> set[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return set()
    return {str(item) for item in required}


def _model_field_default(name: str, schema: Mapping[str, JsonValue], *, required: bool) -> str:
    field_name = _python_name(name)
    alias = "" if field_name == name else f", alias={name!r}"
    if "default" in schema:
        return f" = _Field({schema['default']!r}{alias})"
    if not required:
        return f" = _Field(None{alias})" if alias else " = None"
    return f" = _Field({alias.lstrip(', ')})" if alias else ""


def _contract_operation_lines(
    resource: ResourceManifest,
    *,
    contract: ClientContract,
    context: _SchemaContext,
) -> list[str]:
    operation = contract.operation
    parameters = operation.parameters
    signature = _contract_signature(parameters, context)
    annotation = (
        _annotation_from_json_schema(operation.return_schema, context)
        if operation.return_schema
        else "_JsonValue"
    )
    lines: list[str] = []
    for name in ("remote", "remote_json", "async_remote", "async_remote_json"):
        asynchronous = name.startswith("async_")
        call = _contract_call("async_remote_json" if asynchronous else "remote_json", parameters)
        lines.extend(
            [
                f"    {'async ' if asynchronous else ''}def {name}"
                f"(self{signature}) -> {annotation}:",
                f"        return _TypeAdapter({annotation}).validate_python(",
                f"            {'await ' if asynchronous else ''}{call}",
                "        )",
                "",
            ]
        )
    return lines


def _contract_signature(parameters: list[ClientParameter], context: _SchemaContext) -> str:
    if not parameters:
        return ""
    rendered = [
        (
            f"{_python_name(parameter.name)}: "
            f"{_annotation_from_json_schema(parameter.json_schema, context)}"
            f"{_default_suffix(parameter.required, parameter.default)}"
        )
        for parameter in parameters
    ]
    parts: list[str] = []
    keyword_only = False
    for index, (parameter, value) in enumerate(zip(parameters, rendered, strict=True)):
        if parameter.parameter_kind == "keyword_only" and not keyword_only:
            parts.append("*")
            keyword_only = True
        if parameter.parameter_kind in {"var_positional", "var_keyword"}:
            prefix = "*" if parameter.parameter_kind == "var_positional" else "**"
            annotation = _annotation_from_json_schema(parameter.json_schema, context)
            value = f"{prefix}{_python_name(parameter.name)}: {annotation}"
            keyword_only = True
        parts.append(value)
        if parameter.parameter_kind == "positional_only" and (
            index + 1 == len(parameters)
            or parameters[index + 1].parameter_kind != "positional_only"
        ):
            parts.append("/")
    return ", " + ", ".join(parts)


def _contract_call(handle_method: str, parameters: list[ClientParameter]) -> str:
    positional: list[str] = []
    keywords: list[str] = []
    for parameter in parameters:
        name = _python_name(parameter.name)
        match parameter.parameter_kind:
            case "positional_only" | "keyword":
                positional.append(name)
            case "var_positional":
                positional.append(f"*{name}")
            case "var_keyword":
                keywords.append(f"**{name}")
            case _:
                keywords.append(f"{parameter.name!r}: {name}")
    if keywords:
        positional.append(f"**{{{', '.join(keywords)}}}")
    return f"self._handle.{handle_method}({', '.join(positional)})"


def _annotation_from_json_schema(schema: JsonValue, context: _SchemaContext) -> str:
    schema = validate_json_object(schema)
    if not schema:
        return "_JsonValue"
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in context.ref_names:
            return context.ref_names[ref]
        if ref not in context.definitions:
            raise ClientGenerationError(f"unresolved schema reference: {ref}")
        return _annotation_from_json_schema(context.definitions[ref], context)
    if (key := _schema_key(schema)) in context.schema_names:
        return context.schema_names[key]
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
            return " | ".join(rendered) if rendered else "_JsonValue"
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        rendered = _unique_annotations(
            _annotation_from_json_schema({**schema, "type": item}, context)
            for item in schema_type
            if isinstance(item, str)
        )
        return " | ".join(rendered) if rendered else "_JsonValue"
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
            else "_JsonValue"
        )
        return f"set[{item}]" if schema.get("uniqueItems") is True else f"list[{item}]"
    if schema_type == "object" or "properties" in schema:
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            return f"dict[str, {_annotation_from_json_schema(additional, context)}]"
        return "dict[str, _JsonValue]"
    if "allOf" in schema or "not" in schema:
        raise ClientGenerationError("typed packages do not support this schema composition")
    return "_JsonValue"


def _unique_annotations(values: Iterable[str]) -> list[str]:
    rendered: list[str] = []
    for value in values:
        if value not in rendered:
            rendered.append(value)
    return rendered


def _default_suffix(required: bool, default: JsonValue) -> str:
    if required:
        return ""
    return f" = {default!r}"


def _private_class_name(symbol: str, kind: ResourceKind) -> str:
    return f"_{_python_class_name(symbol)}{_python_class_name(kind.value)}"


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


def _handle_type(kind: ResourceKind) -> str:
    if kind is ResourceKind.Function:
        return "_FunctionHandle"
    raise ValueError(f"unsupported typed client resource kind: {kind.value}")


def _read_lock(output: Path) -> dict[str, JsonValue]:
    path = output / _LOCK_FILE
    if not path.exists():
        return {}
    return parse_json_object(path.read_text(encoding="utf-8"))


def _write_lock(output: Path, lock: dict[str, JsonValue]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / _LOCK_FILE).write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


__all__ = [
    "CLIENT_PACKAGE_ROOT",
    "ClientGenerationError",
    "ClientPackageExport",
    "write_client_package",
]

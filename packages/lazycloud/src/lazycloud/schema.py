from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TypeGuard

from shared.schema import (
    FieldSchema,
    FieldType,
    MediaOutput,
    SchemaDefinition,
    SerializableImage,
    ValidatedImage,
    ValidationError,
    ValueSchema,
    decode_file_input,
    input_object,
    materialize_file,
    validate_boolean,
    validate_integer,
    validate_number,
    validate_string,
)
from typing_extensions import Self

from lazycloud.json_contracts import JsonValue
from lazycloud.session.task import FunctionCall


@dataclass(frozen=True)
class SchemaField:
    type: str
    fields: dict[str, SchemaField] = field(default_factory=dict)

    def validate(self, value: object) -> object:
        with ExitStack() as files:
            result = FieldSchema.from_value(self.to_dict()).validate_value(value, files=files)
            files.pop_all()
            return result

    def dump(self, value: object) -> object:
        with ExitStack() as files:
            return FieldSchema.from_value(self.to_dict()).dump_value(
                value, publish=_publish_media, files=files
            )

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {"type": self.type}
        if self.fields:
            payload["fields"] = {
                name: field_value.to_dict() for name, field_value in self.fields.items()
            }
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> SchemaField:
        parsed = FieldSchema.from_value(dict(value))
        match parsed.type:
            case FieldType.String:
                return String()
            case FieldType.Integer:
                return Integer()
            case FieldType.Number:
                return Number()
            case FieldType.Boolean:
                return Boolean()
            case FieldType.Json:
                return JSON()
            case FieldType.File:
                return File()
            case FieldType.Image:
                return Image(
                    max_size=parsed.max_size,
                    min_size=parsed.min_size,
                    allowed_formats=parsed.allowed_formats,
                    quality=parsed.quality,
                    preserve_metadata=parsed.preserve_metadata,
                )
            case FieldType.Object:
                return Object(
                    {
                        name: SchemaField.from_dict(field.model_dump(mode="json"))
                        for name, field in parsed.fields.items()
                    }
                )


class String(SchemaField):
    def __init__(self) -> None:
        super().__init__("string")

    def validate(self, value: object) -> str:
        return validate_string(value)


class Integer(SchemaField):
    def __init__(self) -> None:
        super().__init__("integer")

    def validate(self, value: object) -> int:
        return validate_integer(value)


class Number(SchemaField):
    def __init__(self) -> None:
        super().__init__("number")

    def validate(self, value: object) -> int | float:
        return validate_number(value)


class Boolean(SchemaField):
    def __init__(self) -> None:
        super().__init__("boolean")

    def validate(self, value: object) -> bool:
        return validate_boolean(value)


class JSON(SchemaField):
    def __init__(self) -> None:
        super().__init__("json")


class File(SchemaField):
    def __init__(self) -> None:
        super().__init__("file")

    def validate(self, value: object) -> IO[bytes]:
        return decode_file_input(materialize_file(value))

    def dump(self, value: object) -> str:
        with ExitStack() as files:
            return FieldSchema.from_value(self.to_dict()).dump_media(
                value, publish=_publish_media, files=files
            )


class Image(SchemaField):
    def __init__(
        self,
        *,
        max_size: tuple[int, int] | None = None,
        min_size: tuple[int, int] | None = None,
        allowed_formats: list[str] | tuple[str, ...] | None = None,
        quality: int = 85,
        preserve_metadata: bool = False,
    ) -> None:
        super().__init__("image")
        self.max_size = max_size
        self.min_size = min_size
        self.allowed_formats = tuple(
            format_name.upper() for format_name in (allowed_formats or ("PNG", "JPEG", "WEBP"))
        )
        self.quality = max(1, min(100, quality))
        self.preserve_metadata = preserve_metadata

    def to_dict(self) -> dict[str, JsonValue]:
        return FieldSchema(
            type=FieldType.Image,
            max_size=self.max_size,
            min_size=self.min_size,
            allowed_formats=self.allowed_formats,
            quality=self.quality,
            preserve_metadata=self.preserve_metadata,
        ).model_dump(mode="json", exclude_defaults=True)

    def validate(self, value: object) -> SerializableImage | ValidatedImage:
        prepared = (
            value
            if isinstance(value, SerializableImage | ValidatedImage)
            else materialize_file(value)
        )
        return FieldSchema.from_value(self.to_dict()).validate_image(prepared)

    def dump(self, value: object) -> str:
        with ExitStack() as files:
            return FieldSchema.from_value(self.to_dict()).dump_media(
                value, publish=_publish_media, files=files
            )


class Object(SchemaField):
    def __init__(self, schema: Schema | Mapping[str, SchemaField]) -> None:
        fields = schema.fields if isinstance(schema, Schema) else dict(schema)
        super().__init__("object", fields=fields)

    def validate(self, value: object) -> dict[str, object]:
        return Schema(self.fields).validate(input_object(value))


@dataclass(frozen=True)
class Schema:
    fields: dict[str, SchemaField] = field(default_factory=dict)

    def validate(self, value: Mapping[str, object]) -> dict[str, object]:
        schema = ValueSchema.from_definition(self)
        prepared = {
            name: _prepare_validation_input(schema.fields[name], item)
            if name in schema.fields
            else item
            for name, item in value.items()
        }
        with ExitStack() as files:
            result = schema.validate_values(prepared, files=files)
            files.pop_all()
            return result

    def dump(self, value: Mapping[str, object]) -> dict[str, object]:
        with ExitStack() as files:
            return ValueSchema.from_definition(self).dump_values(
                value, publish=_publish_media, files=files
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fields": {name: field_value.to_dict() for name, field_value in self.fields.items()}
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> Self:
        schema = ValueSchema.from_definition(value)
        return cls(
            fields={
                name: SchemaField.from_dict(field.model_dump(mode="json"))
                for name, field in schema.fields.items()
            }
        )


def prepare_input_arguments(
    target: Callable[..., object],
    definition: SchemaDefinition,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> tuple[tuple[object, ...], dict[str, object]]:
    bound = ValueSchema.from_definition(definition).transform_arguments(
        target, args, kwargs, _prepare_field_input
    )
    return bound.args, bound.kwargs


def prepare_json_input_arguments(
    target: Callable[..., object],
    definition: SchemaDefinition,
    args: tuple[object, ...],
    kwargs: Mapping[str, object],
) -> tuple[tuple[object, ...], dict[str, object]]:
    args, prepared = prepare_input_arguments(target, definition, args, kwargs)
    bound = ValueSchema.from_definition(definition).transform_arguments(
        target, args, prepared, lambda field, value: field.encode_json_input(value)
    )
    return bound.args, bound.kwargs


def _prepare_field_input(field: FieldSchema, value: object) -> object:
    if _is_deferred_input(value):
        return value
    if field.type is FieldType.File:
        return materialize_file(value)
    if field.type is FieldType.Image:
        if isinstance(value, SerializableImage | ValidatedImage):
            return field.image_output(value).data
        return materialize_file(value)
    if field.type is FieldType.Object:
        values = input_object(value)
        return {
            name: _prepare_field_input(field.fields[name], item) if name in field.fields else item
            for name, item in values.items()
        }
    return value


def _is_deferred_input(value: object) -> TypeGuard[FunctionCall[object]]:
    return isinstance(value, FunctionCall)


def _prepare_validation_input(field: FieldSchema, value: object) -> object:
    if field.type is FieldType.Image and isinstance(value, SerializableImage | ValidatedImage):
        return value
    if field.type is FieldType.Object:
        return {
            name: _prepare_validation_input(field.fields[name], item)
            if name in field.fields
            else item
            for name, item in input_object(value).items()
        }
    return _prepare_field_input(field, value)


def _publish_media(media: MediaOutput) -> str:
    from lazycloud.abstractions.artifact import Artifact

    with tempfile.TemporaryDirectory(prefix="lazycloud-schema-output-") as directory:
        path = Path(directory) / f"output{media.suffix}"
        path.write_bytes(media.data)
        artifact = Artifact.file(path, content_type=media.content_type)
        artifact.save()
        return artifact.public_url()


__all__ = [
    "JSON",
    "Boolean",
    "File",
    "Image",
    "Integer",
    "Number",
    "Object",
    "Schema",
    "SchemaField",
    "String",
    "ValidatedImage",
    "ValidationError",
]

from __future__ import annotations

import base64
import binascii
import errno
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import urlparse

from PIL import Image as PILImage
from PIL.PngImagePlugin import PngInfo
from pydantic import ConfigDict, TypeAdapter, with_config
from pydantic import ValidationError as PydanticValidationError
from shared.errors import InvalidInputError
from typing_extensions import Self, TypedDict

from lazycloud.json_contracts import JsonValue


class ValidationError(InvalidInputError, ValueError):
    def __init__(self, message: str, field: str | None = None) -> None:
        self.message = message
        self.field = field
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        payload = {"error": "ValidationError", "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


class OutputValidationError(ValueError):
    pass


@runtime_checkable
class PublicUrlProvider(Protocol):
    def public_url(self) -> str: ...


@runtime_checkable
class TextPathLike(Protocol):
    def __fspath__(self) -> str: ...


@runtime_checkable
class BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...


@runtime_checkable
class UrlBinaryResponse(Protocol):
    def __enter__(self) -> Self: ...

    def __exit__(self, *args: object) -> None: ...

    def read(self) -> bytes: ...


@dataclass(frozen=True)
class SchemaField:
    type: str
    fields: dict[str, SchemaField] = field(default_factory=dict)

    def validate(self, value: Any) -> Any:
        return value

    def dump(self, value: Any) -> Any:
        return value

    def encode_input(self, value: Any) -> Any:
        return value

    def to_dict(self) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {"type": self.type}
        if self.fields:
            payload["fields"] = {
                name: field_value.to_dict() for name, field_value in self.fields.items()
            }
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> SchemaField:
        field_type = str(value.get("type", ""))
        nested = value.get("fields", {})
        fields: dict[str, SchemaField] = {}
        if not isinstance(nested, dict):
            raise ValidationError("schema fields must be an object")
        for name, field_value in nested.items():
            if not isinstance(field_value, dict):
                raise ValidationError("schema field must be an object", field=name)
            fields[name] = SchemaField.from_dict(field_value)
        match field_type:
            case "string":
                return String()
            case "integer":
                return Integer()
            case "number":
                return Number()
            case "boolean":
                return Boolean()
            case "json":
                return JSON()
            case "file":
                return File()
            case "image":
                options = TypeAdapter[_ImageOptions](_ImageOptions).validate_python(
                    {key: item for key, item in value.items() if key not in {"type", "fields"}}
                )
                return Image(**options)
            case "object":
                return Object(fields)
            case _:
                raise ValidationError(f"unknown schema field type: {field_type!r}")


class String(SchemaField):
    def __init__(self) -> None:
        super().__init__("string")

    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            raise ValidationError(f"expected string, got {type(value).__name__}")
        return value


class Integer(SchemaField):
    def __init__(self) -> None:
        super().__init__("integer")

    def validate(self, value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValidationError(f"expected integer, got {type(value).__name__}")
        return value


class Number(SchemaField):
    def __init__(self) -> None:
        super().__init__("number")

    def validate(self, value: Any) -> int | float:
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValidationError(f"expected number, got {type(value).__name__}")
        return value


class Boolean(SchemaField):
    def __init__(self) -> None:
        super().__init__("boolean")

    def validate(self, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValidationError(f"expected boolean, got {type(value).__name__}")
        return value


class JSON(SchemaField):
    def __init__(self) -> None:
        super().__init__("json")

    def validate(self, value: Any) -> JsonValue:
        return TypeAdapter[JsonValue](
            JsonValue, config=ConfigDict(allow_inf_nan=False)
        ).validate_python(value)


class File(SchemaField):
    def __init__(self) -> None:
        super().__init__("file")

    def validate(self, value: Any) -> Any:
        if isinstance(value, BinaryReader):
            return value
        return BytesIO(_read_media_bytes(value, field="file"))

    def encode_input(self, value: Any) -> Any:
        if isinstance(value, str):
            if _is_url(value) or not _is_file_path(value):
                return value
        elif not isinstance(value, BinaryReader | TextPathLike | bytes | bytearray | memoryview):
            return value
        return base64.b64encode(_read_media_bytes(value, field=self.type)).decode("ascii")

    def dump(self, value: Any) -> str:
        if isinstance(value, str) and _is_url(value):
            return value
        raw = _text_path(value)
        if raw is not None:
            path = Path(raw).expanduser()
            if _is_file_path(raw):
                from lazycloud.abstractions.artifact import Artifact

                artifact = Artifact.file(path)
                artifact.save()
                return artifact.public_url()
        if isinstance(value, PublicUrlProvider):
            return str(value.public_url())

        validated = self.validate(value)
        return _publish_bytes(_binary_reader(validated).read(), suffix=".bin")


@with_config(ConfigDict(extra="forbid"))
class _ImageOptions(TypedDict, total=False):
    max_size: tuple[int, int] | None
    min_size: tuple[int, int] | None
    allowed_formats: list[str] | tuple[str, ...] | None
    quality: int
    preserve_metadata: bool


class Image(File):
    def __init__(
        self,
        *,
        max_size: tuple[int, int] | None = None,
        min_size: tuple[int, int] | None = None,
        allowed_formats: list[str] | tuple[str, ...] | None = None,
        quality: int = 85,
        preserve_metadata: bool = False,
    ) -> None:
        SchemaField.__init__(self, "image")
        self.max_size = max_size
        self.min_size = min_size
        self.allowed_formats = tuple(
            format_name.upper() for format_name in (allowed_formats or ("PNG", "JPEG", "WEBP"))
        )
        self.quality = max(1, min(100, quality))
        self.preserve_metadata = preserve_metadata

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "type": self.type,
            "max_size": list(self.max_size) if self.max_size is not None else None,
            "min_size": list(self.min_size) if self.min_size is not None else None,
            "allowed_formats": list(self.allowed_formats),
            "quality": self.quality,
            "preserve_metadata": self.preserve_metadata,
        }

    def validate(self, value: Any) -> Any:
        if isinstance(value, ValidatedImage):
            value = value.data
        try:
            if isinstance(value, PILImage.Image):
                self._validate_image(value.format or "PNG", value.size)
                value.load()
                return value
            data = _read_media_bytes(value, field="image")
            with PILImage.open(BytesIO(data)) as decoded:
                format_name = decoded.format or ""
                size = decoded.size
                self._validate_image(format_name, size)
                decoded.verify()
            with PILImage.open(BytesIO(data)) as decoded:
                decoded.load()
            return ValidatedImage(data=data, format=format_name, width=size[0], height=size[1])
        except (OSError, SyntaxError, PILImage.DecompressionBombError) as exc:
            raise ValidationError(f"invalid image data: {type(exc).__name__}") from None

    def encode_input(self, value: Any) -> Any:
        if isinstance(value, PILImage.Image):
            data, _ = self._encode_image(value)
            return base64.b64encode(data).decode("ascii")
        if isinstance(value, ValidatedImage):
            return base64.b64encode(value.data).decode("ascii")
        return super().encode_input(value)

    def dump(self, value: Any) -> str:
        if isinstance(value, str) and _is_url(value):
            return value

        image = self.validate(value)
        if isinstance(image, PILImage.Image):
            data, format_name = self._encode_image(image)
        else:
            with PILImage.open(BytesIO(image.data)) as decoded:
                data, format_name = self._encode_image(decoded)
        return _publish_bytes(data, suffix=f".{format_name.lower()}")

    def _encode_image(self, value: PILImage.Image) -> tuple[bytes, str]:
        output_format = (value.format or "PNG").upper()
        self._validate_image(output_format, value.size)
        save_params: dict[str, object] = {"exif": b"", "icc_profile": None, "xmp": b""}
        if self.preserve_metadata:
            for name in ("exif", "icc_profile", "xmp"):
                if name in value.info:
                    save_params[name] = value.info[name]
            if output_format == "PNG":
                metadata = PngInfo()
                for name, item in value.info.items():
                    if isinstance(name, str) and isinstance(item, str):
                        metadata.add_text(name, item)
                save_params["pnginfo"] = metadata
        if output_format in {"JPEG", "WEBP"}:
            save_params["quality"] = self.quality
            if output_format == "JPEG":
                save_params["optimize"] = True
            if output_format == "WEBP":
                save_params["lossless"] = False
        if output_format == "JPEG" and value.mode in {"RGBA", "LA"}:
            value = value.convert("RGB")
        encoded = BytesIO()
        value.save(encoded, format=output_format, **save_params)
        return encoded.getvalue(), output_format

    def _validate_image(self, format_name: str, size: tuple[int, int]) -> None:
        normalized = format_name.upper()
        if normalized == "JPG":
            normalized = "JPEG"
        if normalized and normalized not in self.allowed_formats:
            raise ValidationError(
                f"image format {normalized} is not in allowed formats: {list(self.allowed_formats)}"
            )
        width, height = size
        if self.max_size is not None:
            max_width, max_height = self.max_size
            if width > max_width or height > max_height:
                raise ValidationError(
                    f"image dimensions {width}x{height} exceed maximum {max_width}x{max_height}"
                )
        if self.min_size is not None:
            min_width, min_height = self.min_size
            if width < min_width or height < min_height:
                raise ValidationError(
                    f"image dimensions {width}x{height} are below minimum {min_width}x{min_height}"
                )


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    format: str
    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


class Object(SchemaField):
    def __init__(self, schema: Schema | Mapping[str, SchemaField]) -> None:
        fields = schema.fields if isinstance(schema, Schema) else dict(schema)
        super().__init__("object", fields=fields)

    def validate(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValidationError(f"expected object, got {type(value).__name__}")
        values = TypeAdapter[dict[str, Any]](dict[str, Any]).validate_python(value)
        return Schema(self.fields).validate(values)

    def dump(self, value: Any) -> dict[str, Any]:
        return Schema(self.fields).dump(self.validate(value))

    def encode_input(self, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        values = TypeAdapter[dict[str, Any]](dict[str, Any]).validate_python(value)
        return {
            name: self.fields[name].encode_input(item) if name in self.fields else item
            for name, item in values.items()
        }


@dataclass(frozen=True)
class Schema:
    fields: dict[str, SchemaField] = field(default_factory=dict)

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, field_value in self.fields.items():
            if name not in value:
                raise ValidationError(f"missing required field: {name}", field=name)
            try:
                result[name] = field_value.validate(value[name])
            except ValidationError as exc:
                raise ValidationError(f"{name}: {exc}", field=name) from None
            except PydanticValidationError as exc:
                detail = "; ".join(error["msg"] for error in exc.errors(include_input=False))
                raise ValidationError(f"{name}: {detail}", field=name) from None
        return result

    def dump(self, value: Mapping[str, Any]) -> dict[str, Any]:
        validated = self.validate(value)
        return {
            name: field_value.dump(validated[name]) for name, field_value in self.fields.items()
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fields": {name: field_value.to_dict() for name, field_value in self.fields.items()}
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> Self:
        raw_fields = value.get("fields", value)
        fields: dict[str, SchemaField] = {}
        if not isinstance(raw_fields, Mapping):
            raise ValidationError("schema fields must be an object")
        for name, field_value in raw_fields.items():
            if not isinstance(field_value, dict):
                raise ValidationError("schema field must be an object", field=name)
            fields[name] = SchemaField.from_dict(field_value)
        return cls(fields=fields)


def _publish_bytes(data: bytes, *, suffix: str) -> str:
    from lazycloud.abstractions.artifact import Artifact

    with tempfile.TemporaryDirectory(prefix="lazycloud-output-") as directory:
        path = Path(directory) / f"output{suffix}"
        path.write_bytes(data)
        artifact = Artifact.file(path)
        artifact.save()
        return artifact.public_url()


def _is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return bool(parsed.scheme and parsed.netloc)


def _download_bytes(url: str, *, field: str) -> bytes:
    try:
        with _url_binary_response(url) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise ValidationError(f"failed to download {field} from URL: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ValidationError(
            f"failed to download {field} from URL: {type(exc).__name__}"
        ) from None


def _url_binary_response(url: str) -> UrlBinaryResponse:
    response: object = urllib.request.urlopen(url)
    if not isinstance(response, UrlBinaryResponse):
        raise ValidationError("URL response does not provide binary content")
    return response


def _decode_base64(value: str, *, field: str) -> bytes:
    payload = value.strip()
    if ";base64," in payload:
        payload = payload.split(";base64,", 1)[1]
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError(
            f"{field} string must be an existing path, URL, or base64 encoded data"
        ) from exc


def _binary_reader(value: Any) -> BinaryIO:
    if not isinstance(value, BinaryReader):
        raise ValidationError("expected a binary file-like object")
    position = value.seek(0, 1)
    try:
        value.seek(0)
        return BytesIO(value.read())
    finally:
        value.seek(position)


def _read_media_bytes(value: Any, *, field: str) -> bytes:
    if isinstance(value, BinaryReader):
        reader = _binary_reader(value)
        data = reader.read()
        return data
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    raw = _text_path(value)
    if raw is not None:
        if isinstance(value, str) and _is_url(raw):
            return _download_bytes(raw, field=field)
        if isinstance(value, TextPathLike) or _is_file_path(raw):
            try:
                return Path(raw).expanduser().read_bytes()
            except OSError as exc:
                raise ValidationError(f"failed to read {field} path: {exc.strerror}") from None
        return _decode_base64(raw, field=field)
    type_name = type(value).__name__
    msg = f"expected {field}, path, URL, base64 string, or bytes; got {type_name}"
    raise ValidationError(msg)


def _is_file_path(value: str) -> bool:
    try:
        return Path(value).expanduser().is_file()
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return False
        raise


def _text_path(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, TextPathLike):
        return value.__fspath__()
    return None


__all__ = [
    "JSON",
    "Boolean",
    "File",
    "Image",
    "Integer",
    "Number",
    "Object",
    "OutputValidationError",
    "Schema",
    "SchemaField",
    "String",
    "ValidatedImage",
    "ValidationError",
]

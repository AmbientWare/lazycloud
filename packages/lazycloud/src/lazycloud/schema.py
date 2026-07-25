from __future__ import annotations

import base64
import binascii
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable
from urllib.parse import urlparse

from typing_extensions import Self

from lazycloud.json_contracts import JsonValue


class ValidationError(ValueError):
    def __init__(self, message: str, field: str | None = None) -> None:
        self.message = message
        self.field = field
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        payload = {"error": "ValidationError", "message": self.message}
        if self.field:
            payload["field"] = self.field
        return payload


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


@runtime_checkable
class SerializableImage(Protocol):
    format: str | None
    size: tuple[int, int]
    mode: str

    def save(self, fp: str | Path, *, format: str, **params: object) -> None: ...

    def convert(self, mode: str) -> SerializableImage: ...


@dataclass(frozen=True)
class SchemaField:
    type: str
    fields: dict[str, SchemaField] = field(default_factory=dict)

    def validate(self, value: Any) -> Any:
        return value

    def dump(self, value: Any) -> Any:
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
        if isinstance(nested, dict):
            for name, field_value in nested.items():
                if isinstance(field_value, dict):
                    fields[name] = SchemaField.from_dict(field_value)
        return cls(type=field_type, fields=fields)


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


class File(SchemaField):
    def __init__(self) -> None:
        super().__init__("file")

    def validate(self, value: Any) -> Any:
        if isinstance(value, BinaryReader):
            return value
        raw = _text_path(value)
        if raw is not None:
            path = Path(raw).expanduser()
            if path.is_file():
                return path.open("rb")
            if _is_url(raw):
                return _download_to_tempfile(raw, field="file")
            return _decode_base64_to_file(raw, field="file")
        if isinstance(value, memoryview):
            return BytesIO(value.tobytes())
        if isinstance(value, bytes | bytearray):
            return BytesIO(bytes(value))
        raise ValidationError("expected file-like object, path, URL, base64 string, or bytes")

    def dump(self, value: Any) -> str:
        if isinstance(value, str) and _is_url(value):
            return value
        raw = _text_path(value)
        if raw is not None:
            path = Path(raw).expanduser()
            if path.is_file():
                from lazycloud.abstractions.output import Output

                return Output.file(path).public_url()
        if isinstance(value, PublicUrlProvider):
            return str(value.public_url())

        validated = self.validate(value)
        from lazycloud.abstractions.output import Output

        return Output.from_file(_binary_reader(validated)).public_url()


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

    def validate(self, value: Any) -> Any:
        if isinstance(value, SerializableImage):
            self._validate_image(value.format or "", value.size)
            return value

        data = _read_media_bytes(value, field="image")
        image = _open_image(data)
        self._validate_image(image.format, (image.width, image.height))
        return image

    def dump(self, value: Any) -> str:
        if isinstance(value, str) and _is_url(value):
            return value

        if isinstance(value, SerializableImage):
            return self._dump_serializable_image(value)

        image = value if isinstance(value, ValidatedImage) else self.validate(value)
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{image.format.lower()}") as handle:
            handle.write(image.data)
            path = Path(handle.name)
        try:
            from lazycloud.abstractions.output import Output

            with path.open("rb") as handle:
                return Output.from_file(handle, suffix=path.suffix).public_url()
        finally:
            path.unlink(missing_ok=True)

    def _dump_serializable_image(self, value: SerializableImage) -> str:
        output_format = (value.format or "PNG").upper()
        if output_format not in self.allowed_formats:
            output_format = "PNG"
        save_params: dict[str, object] = {}
        if output_format in {"JPEG", "WEBP"}:
            save_params["quality"] = self.quality
            if output_format == "JPEG":
                save_params["optimize"] = True
            if output_format == "WEBP":
                save_params["lossless"] = False
        if output_format == "JPEG" and value.mode in {"RGBA", "LA"}:
            value = value.convert("RGB")
        suffix = f".{output_format.lower()}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            path = Path(handle.name)
        value.save(path, format=output_format, **save_params)
        try:
            from lazycloud.abstractions.output import Output

            with path.open("rb") as handle:
                return Output.from_file(handle, suffix=path.suffix).public_url()
        finally:
            path.unlink(missing_ok=True)

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


@dataclass(frozen=True)
class Schema:
    fields: dict[str, SchemaField] = field(default_factory=dict)

    def validate(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, field_value in self.fields.items():
            if name not in value:
                raise ValidationError(f"missing required field: {name}", field=name)
            result[name] = field_value.validate(value[name])
        return result

    def dump(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            name: field_value.dump(value[name])
            for name, field_value in self.fields.items()
            if name in value
        }

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "fields": {name: field_value.to_dict() for name, field_value in self.fields.items()}
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, JsonValue]) -> Self:
        raw_fields = value.get("fields", value)
        fields: dict[str, SchemaField] = {}
        if isinstance(raw_fields, Mapping):
            for name, field_value in raw_fields.items():
                if isinstance(name, str) and isinstance(field_value, dict):
                    fields[name] = SchemaField.from_dict(field_value)
        return cls(fields=fields)


def _is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return bool(parsed.scheme and parsed.netloc)


def _download_to_tempfile(url: str, *, field: str) -> BinaryIO:
    name: str | None = None
    try:
        with _url_binary_response(url) as response:
            data = response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ValidationError(f"failed to download {field} from URL: {exc}") from exc
    try:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            name = handle.name
        return Path(name).open("rb")
    except OSError as exc:
        if name is not None:
            Path(name).unlink(missing_ok=True)
        raise ValidationError(f"failed to store downloaded {field}: {exc}") from exc


def _url_binary_response(url: str) -> UrlBinaryResponse:
    response: object = urllib.request.urlopen(url)
    if not isinstance(response, UrlBinaryResponse):
        raise ValidationError("URL response does not provide binary content")
    return response


def _decode_base64_to_file(value: str, *, field: str) -> BinaryIO:
    data = _decode_base64(value, field=field)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            name = handle.name
        return Path(name).open("rb")
    except OSError as exc:
        if name is not None:
            Path(name).unlink(missing_ok=True)
        raise ValidationError(f"failed to store decoded {field}: {exc}") from exc


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
    value.seek(0)
    return BytesIO(value.read())


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
        path = Path(raw).expanduser()
        if path.is_file():
            return path.read_bytes()
        if _is_url(raw):
            with _download_to_tempfile(raw, field=field) as handle:
                return handle.read()
        return _decode_base64(raw, field=field)
    type_name = type(value).__name__
    msg = f"expected image-like object, path, URL, base64 string, or bytes; got {type_name}"
    raise ValidationError(msg)


def _text_path(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, TextPathLike):
        return value.__fspath__()
    return None


def _open_image(data: bytes) -> ValidatedImage:
    return _open_image_header(data)


def _open_image_header(data: bytes) -> ValidatedImage:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return ValidatedImage(data=data, format="PNG", width=width, height=height)
    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_size(data)
        return ValidatedImage(data=data, format="JPEG", width=width, height=height)
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        width, height = _webp_size(data)
        return ValidatedImage(data=data, format="WEBP", width=width, height=height)
    raise ValidationError("invalid image data or unsupported image format")


def _jpeg_size(data: bytes) -> tuple[int, int]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index : index + 2], "big")
        if length < 2:
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(data[index + 3 : index + 5], "big")
            width = int.from_bytes(data[index + 5 : index + 7], "big")
            return width, height
        index += length
    raise ValidationError("invalid JPEG image data")


def _webp_size(data: bytes) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    raise ValidationError("invalid WEBP image data")


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

from __future__ import annotations

import base64
import binascii
import inspect
import io
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Protocol, runtime_checkable
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from shared.contracts import ContractModel
from shared.enums import StringEnum
from shared.serialization import to_json_value

_JSON_VALUE = TypeAdapter[JsonValue](JsonValue)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_INPUT_OBJECT = TypeAdapter(dict[str, object])
FILE_DOWNLOAD_TIMEOUT_SECONDS = 30


class ValidationError(ValueError):
    def __init__(self, message: str, field: str | None = None) -> None:
        self.message = message
        self.field = field
        super().__init__(f"{field}: {message}" if field else message)

    def to_dict(self) -> dict[str, str]:
        result = {"error": "ValidationError", "message": self.message}
        if self.field:
            result["field"] = self.field
        return result


class FieldType(StringEnum):
    String = "string"
    Integer = "integer"
    Number = "number"
    Boolean = "boolean"
    Json = "json"
    Object = "object"
    File = "file"
    Image = "image"


@runtime_checkable
class SchemaExport(Protocol):
    def to_dict(self) -> Mapping[str, JsonValue]: ...


@runtime_checkable
class SchemaModelExport(Protocol):
    def model_dump(self, *, mode: str) -> Mapping[str, JsonValue]: ...


SchemaDefinition = Mapping[str, JsonValue] | SchemaExport | SchemaModelExport


@runtime_checkable
class SchemaCallable(Protocol):
    @property
    def func(self) -> Callable[..., object]: ...

    @property
    def inputs(self) -> SchemaDefinition | None: ...

    @property
    def outputs(self) -> SchemaDefinition | None: ...


@runtime_checkable
class SerializableImage(Protocol):
    format: str | None
    size: tuple[int, int]
    mode: str

    def save(self, fp: str | Path, *, format: str, **params: object) -> None: ...

    def convert(self, mode: str) -> SerializableImage: ...


@runtime_checkable
class BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...

    def tell(self) -> int: ...


@runtime_checkable
class PublicUrlProvider(Protocol):
    def public_url(self) -> str: ...


@runtime_checkable
class TextPathLike(Protocol):
    def __fspath__(self) -> str: ...


@runtime_checkable
class ImageMetadata(Protocol):
    @property
    def info(self) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class ValidatedImage:
    data: bytes
    format: str
    width: int
    height: int

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


@dataclass(frozen=True)
class MediaOutput:
    data: bytes
    suffix: str = ""
    content_type: str = "application/octet-stream"


MediaPublisher = Callable[[MediaOutput], str]


class FieldSchema(ContractModel):
    type: FieldType
    fields: dict[str, FieldSchema] = Field(default_factory=dict)
    max_size: tuple[int, int] | None = None
    min_size: tuple[int, int] | None = None
    allowed_formats: tuple[str, ...] = ("PNG", "JPEG", "WEBP")
    quality: int = Field(default=85, ge=1, le=100)
    preserve_metadata: bool = False

    @classmethod
    def from_value(cls, value: JsonValue) -> FieldSchema:
        if isinstance(value, str):
            value = {"type": value}
        if not isinstance(value, dict):
            raise ValidationError("schema field must declare a type")
        try:
            return cls.model_validate(value)
        except PydanticValidationError:
            raise ValidationError("invalid or unknown input schema field type") from None

    def validate_value(self, value: object, *, files: ExitStack) -> object:
        match self.type:
            case FieldType.String:
                return validate_string(value)
            case FieldType.Integer:
                return validate_integer(value)
            case FieldType.Number:
                return validate_number(value)
            case FieldType.Boolean:
                return validate_boolean(value)
            case FieldType.Json:
                try:
                    return _JSON_VALUE.validate_python(value, strict=True)
                except PydanticValidationError:
                    raise ValidationError("expected a JSON value") from None
            case FieldType.Object:
                return ValueSchema(fields=self.fields).validate_values(value, files=files)
            case FieldType.File:
                return files.enter_context(decode_file_input(value))
            case FieldType.Image:
                return self.validate_image(value)

    def validate_image(self, value: object) -> SerializableImage | ValidatedImage:
        if isinstance(value, SerializableImage | ValidatedImage):
            image = value
        else:
            with decode_file_input(value) as stream:
                image = _open_image_header(stream.read())
        format_name = (image.format or "").upper()
        if format_name == "JPG":
            format_name = "JPEG"
        allowed_formats = tuple(name.upper() for name in self.allowed_formats)
        if format_name and format_name not in allowed_formats:
            raise ValidationError(f"image format {format_name} is not in allowed formats")
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValidationError("image dimensions must be positive")
        if self.max_size is not None:
            max_width, max_height = self.max_size
            if width > max_width or height > max_height:
                raise ValidationError("image dimensions exceed maximum")
        if self.min_size is not None:
            min_width, min_height = self.min_size
            if width < min_width or height < min_height:
                raise ValidationError("image dimensions are below minimum")
        return image

    def image_output(self, value: object) -> MediaOutput:
        image = self.validate_image(value)
        output_format = (image.format or "PNG").upper()
        if output_format == "JPG":
            output_format = "JPEG"
        if isinstance(image, ValidatedImage):
            data = image.data
        else:
            params: dict[str, object] = {}
            if output_format in {"JPEG", "WEBP"}:
                params["quality"] = self.quality
                if output_format == "JPEG":
                    params["optimize"] = True
                else:
                    params["lossless"] = False
            if self.preserve_metadata and isinstance(image, ImageMetadata):
                params.update(
                    (name, image.info[name])
                    for name in ("exif", "icc_profile")
                    if name in image.info
                )
            if output_format == "JPEG" and image.mode in {"RGBA", "LA"}:
                image = image.convert("RGB")
            with tempfile.TemporaryDirectory(prefix="lazycloud-schema-image-") as directory:
                path = Path(directory) / f"image.{output_format.lower()}"
                image.save(path, format=output_format, **params)
                data = path.read_bytes()
        return MediaOutput(
            data=data,
            suffix=f".{output_format.lower()}",
            content_type=f"image/{output_format.lower()}",
        )

    def dump_value(self, value: object, *, publish: MediaPublisher, files: ExitStack) -> object:
        if self.type is FieldType.Object:
            return ValueSchema(fields=self.fields).dump_values(value, publish=publish, files=files)
        if self.type in {FieldType.File, FieldType.Image}:
            return self.dump_media(value, publish=publish, files=files)
        return self.validate_value(value, files=files)

    def dump_media(self, value: object, *, publish: MediaPublisher, files: ExitStack) -> str:
        if isinstance(value, str) and is_http_url(value):
            return value
        if isinstance(value, PublicUrlProvider):
            return value.public_url()
        if self.type is FieldType.Image:
            image_value = (
                value
                if isinstance(value, SerializableImage | ValidatedImage)
                else materialize_file(value)
            )
            media = self.image_output(image_value)
        elif self.type is FieldType.File:
            media = MediaOutput(
                data=files.enter_context(decode_file_input(materialize_file(value))).read()
            )
        else:
            raise ValidationError("media output requires a File or Image field")
        return publish(media)

    def output_json_schema(self) -> dict[str, JsonValue]:
        if self.type is FieldType.Object:
            return ValueSchema(fields=self.fields).output_json_schema()
        if self.type in {FieldType.File, FieldType.Image}:
            return {"type": "string", "format": "uri"}
        if self.type is FieldType.Json:
            return {}
        return {"type": self.type.value}

    def input_json_schema(self) -> dict[str, JsonValue]:
        if self.type is FieldType.Object:
            return {
                "type": "object",
                "properties": {
                    name: field.input_json_schema() for name, field in self.fields.items()
                },
                "required": list(self.fields),
            }
        if self.type in {FieldType.File, FieldType.Image}:
            return {"type": "string", "format": "binary"}
        return self.output_json_schema()

    def encode_json_input(self, value: object) -> JsonValue:
        if self.type in {FieldType.File, FieldType.Image} and isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if self.type is FieldType.Object:
            return {
                name: self.fields[name].encode_json_input(item)
                if name in self.fields
                else to_json_value(item)
                for name, item in input_object(value).items()
            }
        return to_json_value(value)


class ValueSchema(ContractModel):
    fields: dict[str, FieldSchema] = Field(default_factory=dict)

    def output_json_schema(self) -> dict[str, JsonValue]:
        return {
            "type": "object",
            "properties": {name: field.output_json_schema() for name, field in self.fields.items()},
            "required": list(self.fields),
        }

    @classmethod
    def from_definition(cls, value: SchemaDefinition) -> ValueSchema:
        if isinstance(value, SchemaExport):
            raw = value.to_dict()
        elif isinstance(value, SchemaModelExport):
            raw = value.model_dump(mode="json")
        else:
            raw = value
        try:
            metadata = _JSON_OBJECT.validate_python(raw, strict=True)
        except PydanticValidationError:
            raise ValidationError("input schema must be a JSON object") from None
        fields = metadata.get("fields", metadata)
        if not isinstance(fields, dict):
            raise ValidationError("input schema fields must be an object")
        return cls(fields={name: FieldSchema.from_value(field) for name, field in fields.items()})

    def validate_values(self, value: object, *, files: ExitStack) -> dict[str, object]:
        values = input_object(value)
        result: dict[str, object] = {}
        for name, field in self.fields.items():
            if name not in values:
                raise ValidationError(f"missing required field: {name}", field=name)
            try:
                result[name] = field.validate_value(values[name], files=files)
            except ValidationError as exc:
                nested = f"{name}.{exc.field}" if exc.field else name
                raise ValidationError(exc.message, field=nested) from None
        return result

    def dump_values(
        self, value: object, *, publish: MediaPublisher, files: ExitStack
    ) -> dict[str, object]:
        values = input_object(value)
        result: dict[str, object] = {}
        for name, field in self.fields.items():
            if name not in values:
                raise ValidationError(f"missing required field: {name}", field=name)
            try:
                result[name] = field.dump_value(values[name], publish=publish, files=files)
            except ValidationError as exc:
                nested = f"{name}.{exc.field}" if exc.field else name
                raise ValidationError(exc.message, field=nested) from None
        return result

    def transform_arguments(
        self,
        target: Callable[..., object],
        args: tuple[object, ...],
        kwargs: Mapping[str, object],
        transform: Callable[[FieldSchema, object], object],
    ) -> inspect.BoundArguments:
        signature = inspect.signature(target)
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError as exc:
            raise ValidationError(str(exc)) from None
        bound.apply_defaults()
        extra_name = next(
            (
                name
                for name, parameter in signature.parameters.items()
                if parameter.kind is inspect.Parameter.VAR_KEYWORD
            ),
            None,
        )
        extras = input_object(bound.arguments[extra_name]) if extra_name is not None else {}
        for name, field in self.fields.items():
            values = bound.arguments if name in bound.arguments else extras
            if name not in values:
                raise ValidationError(f"missing required field: {name}", field=name)
            try:
                values[name] = transform(field, values[name])
            except ValidationError as exc:
                nested = f"{name}.{exc.field}" if exc.field else name
                raise ValidationError(exc.message, field=nested) from None
        if extra_name is not None:
            bound.arguments[extra_name] = extras
        return bound


def input_object(value: object) -> dict[str, object]:
    try:
        return _INPUT_OBJECT.validate_python(value, strict=True)
    except PydanticValidationError:
        raise ValidationError("expected an object with string keys") from None


def validate_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"expected string, got {type(value).__name__}")
    return value


def validate_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"expected integer, got {type(value).__name__}")
    return value


def validate_number(value: object) -> int | float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValidationError(f"expected number, got {type(value).__name__}")
    return value


def validate_boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"expected boolean, got {type(value).__name__}")
    return value


def decode_file_input(value: object) -> IO[bytes]:
    if isinstance(value, memoryview):
        return io.BytesIO(value.tobytes())
    if isinstance(value, bytes | bytearray):
        return io.BytesIO(bytes(value))
    if not isinstance(value, str):
        raise ValidationError("expected file bytes, base64 data, or an HTTP URL")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValidationError("invalid file input") from None
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return _download_file(value)
    encoded = value
    if value.startswith("data:") and ";base64," in value:
        encoded = value.split(";base64,", 1)[1]
    try:
        return io.BytesIO(base64.b64decode(encoded, validate=True))
    except (ValueError, binascii.Error):
        raise ValidationError("file string must be base64 data or an HTTP URL") from None


def materialize_file(value: object) -> bytes | str:
    if isinstance(value, BinaryReader):
        position = value.tell()
        try:
            value.seek(0)
            return value.read()
        finally:
            value.seek(position)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if isinstance(value, TextPathLike):
        path = Path(value.__fspath__()).expanduser()
        try:
            return path.read_bytes()
        except OSError:
            raise ValidationError("could not read file input path") from None
    if not isinstance(value, str):
        raise ValidationError("expected file-like object, path, HTTP URL, base64 data, or bytes")
    if is_http_url(value):
        return value
    try:
        path = Path(value).expanduser()
        if path.is_file():
            return path.read_bytes()
    except OSError:
        pass
    with decode_file_input(value) as stream:
        return stream.read()


def is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValidationError("invalid file input") from None
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


class _HttpFileRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlsplit(newurl).scheme not in {"http", "https"}:
            raise ValidationError("file URL redirects must use HTTP or HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_file(url: str) -> IO[bytes]:
    with ExitStack() as files:
        stream = files.enter_context(
            tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        )
        try:
            opener = urllib.request.build_opener(_HttpFileRedirect())
            with opener.open(url, timeout=FILE_DOWNLOAD_TIMEOUT_SECONDS) as response:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            stream.seek(0)
        except (urllib.error.URLError, OSError, ValueError):
            raise ValidationError("file URL download failed") from None
        files.pop_all()
        return stream


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
    "BinaryReader",
    "FieldSchema",
    "FieldType",
    "MediaOutput",
    "MediaPublisher",
    "SchemaCallable",
    "SchemaDefinition",
    "SerializableImage",
    "ValidatedImage",
    "ValidationError",
    "ValueSchema",
    "decode_file_input",
    "input_object",
    "materialize_file",
    "validate_boolean",
    "validate_integer",
    "validate_number",
    "validate_string",
]

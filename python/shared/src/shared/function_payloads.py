from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import Field, model_validator

from shared.bytes_transport import EncodedBytesBody, encode_bytes
from shared.contracts import ContractModel
from shared.enums import StringEnum

FUNCTION_RESULT_DISPLAY_TEXT_MAX_CHARS = 64 * 1024
FUNCTION_RESULT_DISPLAY_HTML_MAX_CHARS = 256 * 1024
FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES = 1024 * 1024
FUNCTION_RESULT_DISPLAY_IMAGE_BASE64_MAX_CHARS = (
    (FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES + 2) // 3
) * 4


class FunctionPayloadEncoding(StringEnum):
    Json = "json"
    Cloudpickle = "cloudpickle"


class FunctionResultDisplayKind(StringEnum):
    Html = "html"
    Image = "image"


class FunctionResultHtmlDisplay(ContractModel):
    kind: Literal[FunctionResultDisplayKind.Html] = FunctionResultDisplayKind.Html
    html: str = Field(min_length=1, max_length=FUNCTION_RESULT_DISPLAY_HTML_MAX_CHARS)


class FunctionResultImageDisplay(EncodedBytesBody):
    kind: Literal[FunctionResultDisplayKind.Image] = FunctionResultDisplayKind.Image
    media_type: Literal["image/png"] = "image/png"
    value_base64: str = Field(default="", max_length=FUNCTION_RESULT_DISPLAY_IMAGE_BASE64_MAX_CHARS)
    size_bytes: int = Field(ge=1, le=FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES)

    @classmethod
    def from_bytes(cls, value: bytes) -> FunctionResultImageDisplay:
        if not value or len(value) > FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES:
            raise ValueError(
                f"result image must be 1 to {FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES} bytes"
            )
        return cls(value_base64=encode_bytes(value), size_bytes=len(value))

    @model_validator(mode="after")
    def validate_content(self) -> FunctionResultImageDisplay:
        if len(self.bytes_value()) != self.size_bytes:
            raise ValueError("result image size does not match its payload")
        return self


FunctionResultRichDisplay: TypeAlias = Annotated[
    FunctionResultHtmlDisplay | FunctionResultImageDisplay,
    Field(discriminator="kind"),
]


class FunctionResultDisplay(ContractModel):
    text: str = Field(max_length=FUNCTION_RESULT_DISPLAY_TEXT_MAX_CHARS)
    rich: FunctionResultRichDisplay | None = None


__all__ = [
    "FUNCTION_RESULT_DISPLAY_HTML_MAX_CHARS",
    "FUNCTION_RESULT_DISPLAY_IMAGE_MAX_BYTES",
    "FUNCTION_RESULT_DISPLAY_TEXT_MAX_CHARS",
    "FunctionPayloadEncoding",
    "FunctionResultDisplay",
    "FunctionResultDisplayKind",
    "FunctionResultHtmlDisplay",
    "FunctionResultImageDisplay",
    "FunctionResultRichDisplay",
]

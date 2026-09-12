from __future__ import annotations

import base64
import traceback
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import IO

import pytest
from lazycloud.abstractions.artifact import ArtifactNotSavedError
from lazycloud.schema import (
    File,
    Image,
    Integer,
    Object,
    Schema,
    String,
    ValidatedImage,
    prepare_input_arguments,
)
from lazycloud.session import Client
from lazycloud.session.task import FunctionCall
from runner.function import decode_function_invocation
from runner.invocation import OutputValidationError, cloudpickle_bytes, invoke_handler
from runner.serve import EndpointServeRunner
from shared.function_payloads import FunctionCloudpickleInvocation
from shared.http.functions import FunctionClaimedTask
from shared.schema import ValidationError
from tests.fakes import FakeDeploymentClient
from tests.http_server import running_http_server

from lazycloud import App


@App("http_schema").endpoint(
    inputs=Schema({"file": File(), "nested": Object({"image": Image()}), "value": Integer()})
)
def http_media(
    file: IO[bytes], nested: dict[str, object], value: float = 7
) -> dict[str, str | int]:
    image = nested["image"]
    assert isinstance(image, ValidatedImage)
    return {
        "file": base64.b64encode(file.read()).decode(),
        "width": image.width,
        "value_type": type(value).__name__,
    }


def test_explicit_schema_controls_bound_defaults_and_nested_validation() -> None:
    entered: list[int] = []
    schema = Schema({"value": Integer(), "nested": Object({"name": String()})})

    @App("strict_inputs").function(inputs=Schema.from_dict(schema.to_dict()))
    def handler(value: float = 7, *, nested: dict[str, str]) -> tuple[float, str, str]:
        entered.append(1)
        return value, type(value).__name__, nested["name"]

    assert invoke_handler(handler, (), {"nested": {"name": "kept"}}) == (7, "int", "kept")
    assert invoke_handler(handler, (9,), {"nested": {"name": "positional"}}) == (
        9,
        "int",
        "positional",
    )
    with pytest.raises(ValidationError, match="value: expected integer"):
        invoke_handler(handler, (True,), {"nested": {"name": "rejected"}})
    with pytest.raises(ValidationError, match=r"nested\.name: expected string"):
        invoke_handler(handler, (3,), {"nested": {"name": False}})
    with pytest.raises(ValidationError, match=r"nested\.name: missing required field"):
        invoke_handler(handler, (3,), {"nested": {}})
    assert len(entered) == 2
    with pytest.raises(ValidationError, match="unknown input schema"):
        Schema.from_dict({"fields": {"value": {"type": "unknown"}}})


def test_file_input_materializes_before_transport_and_closes_after_execution(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input.bin"
    expected = bytes(range(256)) * 7
    source.write_bytes(expected)
    consumed: list[IO[bytes]] = []
    schema = Schema({"file": File()})

    @App("file_inputs").function(inputs=schema)
    async def handler(file: IO[bytes], *, fail: bool = False) -> bytes:
        consumed.append(file)
        if fail:
            raise RuntimeError("handler failed")
        return file.read()

    with source.open("rb") as reader:
        reader.seek(4)
        reader_args, _ = prepare_input_arguments(handler.func, schema, (reader,), {})
        assert reader.tell() == 4 and reader_args == (expected,)
    args, kwargs = prepare_input_arguments(handler.func, schema, (source,), {})
    envelope = FunctionCloudpickleInvocation.from_bytes(
        cloudpickle_bytes({"args": args, "kwargs": kwargs})
    )
    source.unlink()
    invocation = decode_function_invocation(
        FunctionClaimedTask(task_id="file-input", invocation=envelope)
    )
    assert invoke_handler(handler, invocation.args, invocation.kwargs) == expected
    assert consumed[-1].closed
    with pytest.raises(RuntimeError, match="handler failed"):
        invoke_handler(handler, (expected,), {"fail": True})
    assert consumed[-1].closed

    deferred = FunctionCall[object]("upstream-file", Client().task_client)
    pending, _ = prepare_input_arguments(handler.func, schema, (deferred,), {})
    assert pending[0] is deferred
    with BytesIO(expected) as reader:
        reader.seek(4)
        with pytest.raises(ArtifactNotSavedError):
            File().dump(reader)
        assert not reader.closed and reader.tell() == 4


def test_file_runtime_decodes_http_and_base64_without_reading_container_paths(
    tmp_path: Path,
) -> None:
    expected = b"file-url-and-base64-input"
    source = tmp_path / "input.bin"
    source.write_bytes(expected)
    consumed: list[IO[bytes]] = []

    @App("remote_file_inputs").endpoint(inputs=Schema({"file": File()}))
    def handler(file: IO[bytes]) -> bytes:
        consumed.append(file)
        return file.read()

    class FileHandler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FileHandler, directory=str(tmp_path)))
    server.daemon_threads = True
    with running_http_server(server):
        url = f"http://127.0.0.1:{server.server_port}/input.bin"
        assert invoke_handler(handler, (), {"file": url}) == expected
        assert consumed[-1].closed
        failed_url = f"http://127.0.0.1:{server.server_port}/missing?token=private-query"
        with pytest.raises(ValidationError, match="file URL download failed"):
            try:
                invoke_handler(handler, (), {"file": failed_url})
            except ValidationError:
                assert "private-query" not in traceback.format_exc()
                raise

    assert invoke_handler(handler, (base64.b64encode(expected).decode(),), {}) == expected
    assert consumed[-1].closed
    with pytest.raises(ValidationError, match="file string must be base64"):
        invoke_handler(handler, (str(source),), {})
    assert source.read_bytes() == expected


def test_image_schema_retains_options_and_outputs_validate_returned_mapping(tmp_path: Path) -> None:
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
        "WjR9awAAAABJRU5ErkJggg=="
    )
    schema = Schema(
        {
            "image": Image(
                min_size=(1, 1),
                max_size=(1, 1),
                allowed_formats=["PNG"],
                quality=93,
                preserve_metadata=True,
            )
        }
    )
    restored = Schema.from_dict(schema.to_dict())
    assert restored.to_dict() == schema.to_dict()

    @App("image_schema").function(inputs=restored, outputs=Schema({"width": Integer()}))
    def handler(image: ValidatedImage) -> dict[str, object]:
        return {"width": image.width, "private": "excluded"}

    path = tmp_path / "pixel.png"
    path.write_bytes(data)
    args, kwargs = prepare_input_arguments(handler.func, restored, (path,), {})
    path.unlink()
    assert invoke_handler(handler, args, kwargs) == {"width": 1}
    wrong_dimensions = data[:16] + (2).to_bytes(4, "big") + data[20:]
    with pytest.raises(ValidationError, match="image dimensions exceed maximum"):
        invoke_handler(handler, (wrong_dimensions,), {})

    @App("invalid_output").function(outputs=Schema({"value": Integer()}))
    def invalid(value: object) -> object:
        return value

    with pytest.raises(OutputValidationError, match="expected an object"):
        invoke_handler(invalid, (1,), {})
    with pytest.raises(OutputValidationError, match="value: expected integer"):
        invoke_handler(invalid, ({"value": True},), {})


def test_endpoint_request_materializes_media_before_json_transport(tmp_path: Path) -> None:
    image = tmp_path / "pixel.png"
    image.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGA"
            "WjR9awAAAABJRU5ErkJggg=="
        )
    )
    runner = EndpointServeRunner(handler_ref=f"{__name__}:http_media", host="127.0.0.1", port=0)
    runner._handler = http_media
    server = runner.create_server()
    http_media.deployment_client = FakeDeploymentClient(
        invoke_url_template=f"http://127.0.0.1:{server.server_port}/invoke"
    )
    try:
        with running_http_server(server), BytesIO(b"endpoint media\x00\xff") as reader:
            reader.seek(4)
            response = http_media.target("deployed").request(reader, {"image": image})
            assert response.status_code == 200
            assert response.json() == {
                "file": base64.b64encode(reader.getvalue()).decode(),
                "width": 1,
                "value_type": "int",
            }
            assert reader.tell() == 4 and not reader.closed
            invalid = http_media.target("deployed").request(reader, {"image": image}, True)
            assert invalid.status_code == 400
    finally:
        runner.control.close()
        http_media.deployment_client = None

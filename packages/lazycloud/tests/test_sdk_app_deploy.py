from __future__ import annotations

from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Lock
from urllib.parse import urlsplit

from pydantic import BaseModel
from shared.app_identity import SOURCE_PACKAGE_BUCKET
from shared.http.gateway import (
    DeployStubRequest,
    DeployStubResponse,
    GetOrCreateStubRequest,
    GetOrCreateStubResponse,
)
from shared.http.images import BuildImageRequest, BuildImageResponse, VerifyImageBuildResponse
from shared.http.objects import (
    BeginObjectUploadResponse,
    HeadObjectRequest,
    HeadObjectResponse,
    PutObjectRequest,
)
from tests.fakes import FakeDeploymentClient
from tests.http_server import running_http_server

from lazycloud import App, Image


def test_app_deploy_applies_the_pool_to_every_deployable_resource() -> None:
    app = App("pool_deploy")
    deployments = FakeDeploymentClient(stub_id_from_type=True)

    function = app.function(lambda: "function", name="function")
    endpoint = app.endpoint(name="endpoint")(lambda: "endpoint")
    pod = app.pod(name="pod")

    async def asgi_handler(
        scope: dict[str, object],
        receive: Callable[[], Awaitable[dict[str, object]]],
        send: Callable[[dict[str, object]], Awaitable[None]],
    ) -> None:
        del scope, receive, send

    asgi = app.asgi(name="asgi")(asgi_handler)

    function.deployment_client = deployments
    endpoint.deployment_client = deployments
    pod.deployment_client = deployments
    asgi.deployment_client = deployments

    result = app.deploy(
        workspace="production",
        pool="aws",
    )

    assert len(result.resources) == 4
    assert {request.name for request in deployments.stub_requests} == {
        "function",
        "endpoint",
        "pod",
        "asgi",
    }
    assert all(
        request.workspace == "production" and request.pool == "aws"
        for request in deployments.stub_requests
    )


def test_app_deploy_pool_only_changes_the_selected_resource() -> None:
    app = App("selected_pool")
    deployments = FakeDeploymentClient(stub_id_from_type=True)
    function = app.function(
        lambda: "function",
        name="function",
    )
    pod = app.pod(name="pod", pool="lazycloud")
    function.deployment_client = deployments

    app.deploy(
        resource="function:function",
        pool="aws",
    )

    assert function.pool == "aws"
    assert pod.pool == "lazycloud"
    assert len(deployments.stub_requests) == 1
    assert deployments.stub_requests[0].pool == "aws"


def test_app_deploy_overlaps_builds_and_uploads_and_shares_only_one_source_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_file = source / "value.txt"
    source_file.write_text("first snapshot")
    context = tmp_path / "context"
    context.mkdir()
    dockerfile = context / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\n")
    overlap = Barrier(3, timeout=5)
    lock = Lock()
    stored: dict[tuple[str, str], str] = {}
    uploads: list[str] = []
    builds: list[BuildImageRequest] = []
    stubs: list[GetOrCreateStubRequest] = []
    deployment_pass = 0

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            parsed = urlsplit(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            response: BaseModel
            if parsed.path == "/gateway/objects/uploads":
                upload_request = PutObjectRequest.model_validate_json(body)
                bucket = upload_request.bucket
                digest = upload_request.hash
                with lock:
                    stored[bucket, digest] = digest
                    uploads.append(bucket)
                if bucket == SOURCE_PACKAGE_BUCKET and deployment_pass == 0:
                    source_file.write_text("next snapshot")
                    overlap.wait()
                response = BeginObjectUploadResponse(object_id=digest)
            elif parsed.path == "/gateway/objects/head":
                request = HeadObjectRequest.model_validate_json(body)
                with lock:
                    object_id = stored.get((request.bucket, request.hash), "")
                response = HeadObjectResponse(exists=bool(object_id), object_id=object_id)
            elif parsed.path == "/api/v1/images/verify-build":
                response = VerifyImageBuildResponse(image_id="", valid=True, exists=False)
            elif parsed.path == "/api/v1/images/build":
                request = BuildImageRequest.model_validate_json(body)
                with lock:
                    builds.append(request)
                if deployment_pass == 0:
                    overlap.wait()
                response = BuildImageResponse(
                    done=True, success=True, image_id="built-image", python_version="python3.12"
                )
            elif parsed.path == "/gateway/stubs/get-or-create":
                request = GetOrCreateStubRequest.model_validate_json(body)
                with lock:
                    stubs.append(request)
                response = GetOrCreateStubResponse(stub_id=request.name)
            elif parsed.path == "/gateway/stubs/deploy":
                request = DeployStubRequest.model_validate_json(body)
                response = DeployStubResponse(
                    stub_id=request.stub_id, deployment_id=request.name, version=1
                )
            else:
                raise AssertionError(parsed.path)
            encoded = (response.model_dump_json() + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    app = App("parallel_preparation")
    pods = [
        app.pod(name="first", image=Image.from_dockerfile(dockerfile)),
        app.pod(name="second", image=Image.from_dockerfile(dockerfile)),
        app.pod(name="third", image=Image().add_commands(["echo distinct"])),
    ]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    with running_http_server(server):
        for pod in pods:
            pod.endpoint = f"http://127.0.0.1:{server.server_port}"
            pod.token = "test-token"
        for deployment_pass in range(2):
            result = app.deploy(workspace="tenant-a", source_root=source)
            assert len(builds) == 2 * (deployment_pass + 1)
            assert [
                resource.deployment_id
                for resource in result.resources
                if isinstance(resource, DeployStubResponse)
            ] == ["first", "second", "third"]

    assert len(builds) == 4
    assert uploads.count(SOURCE_PACKAGE_BUCKET) == 2
    assert len(uploads) == 3
    assert len({stub.object_id for stub in stubs[:3]}) == 1
    assert len({stub.object_id for stub in stubs[3:]}) == 1
    assert stubs[0].object_id != stubs[3].object_id

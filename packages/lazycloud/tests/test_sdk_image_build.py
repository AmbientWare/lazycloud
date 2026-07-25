from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.image_building.credentials import ImageCredentialLookupError

from lazycloud import Image


@dataclass
class FakeImageControlClient:
    responses: list[BuildImageResponse]
    requests: list[BuildImageRequest] = field(default_factory=list)

    def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]:
        self.requests.append(request)
        yield from self.responses


@dataclass
class FakeCachedImageControlClient:
    verify_responses: list[VerifyImageBuildResponse]
    build_responses: list[BuildImageResponse] = field(default_factory=list)
    verify_requests: list[VerifyImageBuildRequest] = field(default_factory=list)
    build_requests: list[BuildImageRequest] = field(default_factory=list)

    def verify_image_build(self, request: VerifyImageBuildRequest) -> VerifyImageBuildResponse:
        self.verify_requests.append(request)
        return self.verify_responses.pop(0)

    def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]:
        self.build_requests.append(request)
        yield from self.build_responses


@dataclass
class FakeImageVerifyClient:
    responses: list[VerifyImageBuildResponse]
    requests: list[VerifyImageBuildRequest] = field(default_factory=list)

    def verify_image_build(self, request: VerifyImageBuildRequest) -> VerifyImageBuildResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def test_sdk_image_build_request_preserves_credentials_without_leaking_spec_values() -> None:
    image = (
        Image.from_registry(
            "ghcr.io/team/worker:latest",
            credentials={"username": "user", "password": "secret"},
        )
        .add_python_packages(["httpx >= 0.27"])
        .add_commands(["python -m compileall /workspace"])
        .with_envs({"MODE": "test"})
        .with_secrets(["API_TOKEN"])
        .build_with_gpu("A10G")
    )

    request = image._build_request()
    verify_request = image._verify_request()
    spec_payload = image.spec().model_dump(mode="json")

    assert request.existing_image_uri == "ghcr.io/team/worker:latest"
    assert request.existing_image_creds == {"username": "user", "password": "secret"}
    assert verify_request.existing_image_creds == request.existing_image_creds
    assert request.build_steps[0].type == "pip"
    assert request.build_steps[0].command == "httpx>=0.27"
    assert request.build_steps[1].type == "shell"
    assert request.env_vars == ["MODE=test"]
    assert request.secrets == ["API_TOKEN"]
    assert request.gpu == "A10G"
    assert spec_payload["credential_keys"] == ["username", "password"]
    assert request.existing_image_creds["password"] not in spec_payload["credential_keys"]
    assert request.existing_image_creds["password"] not in spec_payload["secrets"]


def test_sdk_image_with_docker_selects_only_supported_official_repositories() -> None:
    request = Image().with_docker()._build_request()
    commands = [step.command for step in request.build_steps]
    repository_commands = [
        command for command in commands if "download.docker.com/linux" in command
    ]

    assert len(repository_commands) == 2
    assert all(
        '. /etc/os-release; case "$ID" in debian|ubuntu)' in command
        for command in repository_commands
    )
    assert all("download.docker.com/linux/$ID" in command for command in repository_commands)
    assert all("download.docker.com/linux/ubuntu" not in command for command in repository_commands)
    assert all(
        "supports only Debian and Ubuntu base images" in command for command in repository_commands
    )
    assert "VERSION_CODENAME" in repository_commands[-1]
    runtime_install = next(
        command for command in commands if "apt-get install -y docker-ce" in command
    )
    assert all(
        package in runtime_install
        for package in (
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-buildx-plugin",
            "docker-compose-plugin",
        )
    )


def test_sdk_image_build_context_rejects_symlink_to_outside_file(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be uploaded", encoding="utf-8")
    (context / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (context / "secret.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="build context symlinks are not allowed"):
        Image.from_dockerfile(context / "Dockerfile", context_dir=context)._context_archive()


def test_sdk_image_build_request_resolves_env_credentials() -> None:
    image = Image.from_registry(
        "ghcr.io/team/worker:latest",
        credentials=["GITHUB_TOKEN"],
    )

    request = image._build_request(env={"GITHUB_TOKEN": "gh-secret"})

    assert request.existing_image_creds == {"GITHUB_TOKEN": "gh-secret"}
    with pytest.raises(ImageCredentialLookupError):
        image._build_request(env={})


def test_sdk_reads_google_service_account_file_before_transport(tmp_path: Path) -> None:
    credentials_path = tmp_path / "service-account.json"
    credentials_path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "project",
                "client_email": "builder@example.iam.gserviceaccount.com",
                "private_key": "private-key-material",
            }
        ),
        encoding="utf-8",
    )
    image = Image.from_registry(
        "us-docker.pkg.dev/project/repository/app:latest",
        credentials=["GOOGLE_APPLICATION_CREDENTIALS"],
    )

    request = image._build_request(env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)})

    transported = request.existing_image_creds["GOOGLE_APPLICATION_CREDENTIALS"]
    assert json.loads(transported)["client_email"] == ("builder@example.iam.gserviceaccount.com")
    assert str(credentials_path) not in transported


def test_sdk_rejects_invalid_google_service_account_file(tmp_path: Path) -> None:
    credentials_path = tmp_path / "not-service-account.json"
    credentials_path.write_text('{"type":"authorized_user"}', encoding="utf-8")
    image = Image.from_registry(
        "gcr.io/project/app:latest",
        credentials=["GOOGLE_APPLICATION_CREDENTIALS"],
    )

    with pytest.raises(ValueError, match="service-account JSON"):
        image._build_request(env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)})


def test_sdk_image_uv_project_maps_to_build_request_and_context(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = ["fastapi >= 0.115"]

[project.optional-dependencies]
worker = ["redis >= 5"]
""",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("# lock\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print('not copied')\n", encoding="utf-8")

    image = Image().add_uv_project(tmp_path, extras=["worker"])

    request = image._build_request()
    archive = image._context_archive()

    assert [step.type for step in request.build_steps] == ["uv-project"]
    assert request.build_steps[0].command == ". worker"
    assert archive.files == ("pyproject.toml", "uv.lock")


def test_sdk_image_uv_project_requires_lockfile(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"uv\.lock"):
        Image().add_uv_project(tmp_path)


def test_sdk_image_build_revalidates_durable_identity_before_each_build() -> None:
    reusable = VerifyImageBuildResponse(
        image_id="img_cached",
        valid=True,
        exists=True,
        build_id="build_cached",
        cache_key="cache_cached",
    )
    client = FakeCachedImageControlClient(
        verify_responses=[reusable, reusable],
    )
    image = Image().add_python_packages(["cached-image-test-package==1.0.0"])

    first = image.build(client)
    second = image.build(client)

    assert first.success is True
    assert first.image_id == "img_cached"
    assert second == first
    assert len(client.verify_requests) == 2
    assert client.build_requests == []

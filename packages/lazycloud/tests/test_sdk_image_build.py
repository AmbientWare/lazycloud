from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from lazycloud.terminal import Terminal
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.image_building.credentials import ImageCredentialLookupError

from lazycloud import Image, output


@pytest.mark.parametrize("success", [False, True])
def test_sdk_build_output_is_opt_in_and_uses_stderr(
    capsys: pytest.CaptureFixture[str],
    success: bool,
) -> None:
    terminal = Terminal(default_enabled=False)
    lines = [f"build-line-{index}" for index in range(6)]
    responses = [BuildImageResponse(msg=f"{line}\n") for line in lines]
    responses.append(
        BuildImageResponse(
            done=True,
            success=success,
            image_id="image-built" if success else "",
            error="build failed" if not success else "",
        )
    )
    for enabled in (None, True, False):
        client = FakeCachedImageControlClient(
            verify_responses=[VerifyImageBuildResponse(image_id="", valid=True, exists=False)],
            build_responses=responses,
        )
        with output(enabled=enabled) if enabled is not None else nullcontext():
            result = Image().build(client, terminal=terminal)
        assert result.success is success
        assert result.error == ("" if success else "build failed")
        captured = capsys.readouterr()
        assert captured.out == ""
        visible = enabled is True
        if visible:
            for line in lines:
                assert captured.err.count(line) == 1
        else:
            assert captured.err == ""


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
    assert request.env_vars == ["MODE=test"]
    assert request.secrets == ["API_TOKEN"]
    assert request.gpu == "A10G"
    assert spec_payload["credential_keys"] == ["username", "password"]
    assert request.existing_image_creds["password"] not in spec_payload["credential_keys"]
    assert request.existing_image_creds["password"] not in spec_payload["secrets"]


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


def test_sdk_image_uv_project_requires_lockfile(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"uv\.lock"):
        Image.from_uv(tmp_path)


def _write_uv_project(root: Path, *, members: tuple[str, ...] = ()) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "[project]\nname='demo'\nversion='0'\nrequires-python='>=3.12'\n",
        encoding="utf-8",
    )
    packages = ["[[package]]\nname = 'demo'\nsource = { virtual = '.' }\n"]
    for member in members:
        name = Path(member).name
        packages.append(f"[[package]]\nname = '{name}'\nsource = {{ editable = '{member}' }}\n")
    (root / "uv.lock").write_text("version = 1\n" + "\n".join(packages), encoding="utf-8")
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")


def test_sdk_image_uv_project_identity_covers_only_what_the_build_reads(tmp_path: Path) -> None:
    _write_uv_project(tmp_path)
    before = Image.from_uv(tmp_path)
    before_files = before._context_archive().files

    (tmp_path / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("scratch\n", encoding="utf-8")
    after_source_edit = Image.from_uv(tmp_path)

    assert after_source_edit.spec().context_digest == before.spec().context_digest
    assert after_source_edit._context_archive().files == before_files
    assert before_files == ("pyproject.toml", "uv.lock")

    (tmp_path / "uv.lock").write_text(
        (tmp_path / "uv.lock").read_text(encoding="utf-8") + "\n[[package]]\nname = 'httpx'\n",
        encoding="utf-8",
    )

    assert Image.from_uv(tmp_path).spec().context_digest != before.spec().context_digest


def test_sdk_image_uv_project_includes_workspace_members_inside_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    _write_uv_project(root, members=("packages/member",))
    member = root / "packages" / "member"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text("[project]\nname='member'\n", encoding="utf-8")
    (member / "member.py").write_text("VALUE = 1\n", encoding="utf-8")
    (member / ".lazycloudignore").write_text("scratch/\n", encoding="utf-8")
    (member / "scratch").mkdir()
    (member / "scratch" / "big.bin").write_bytes(b"\0" * 16)

    files = Image.from_uv(root)._context_archive().files

    assert files == (
        "packages/member/.lazycloudignore",
        "packages/member/member.py",
        "packages/member/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
    )

    outside = tmp_path / "outside"
    _write_uv_project(outside, members=("../root/packages/member",))

    with pytest.raises(ValueError, match="outside the project root"):
        Image.from_uv(outside)


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

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from lazycloud._shared.image_building.credentials import ImageCredentialLookupError
from lazycloud.exceptions import UnsupportedFeatureError
from lazycloud.terminal import Terminal, TerminalStep

from lazycloud import Image, LinuxArchitecture, output
from tests.api_server import ApiRequest, FakeApi, Reply, StreamAborted, error_reply, json_reply

NOW = "2026-09-30T12:00:00Z"
IMAGE_ID = "img_0123456789abcdef01234567"
BUILD_ID = "0192f0a0-0000-7000-8000-0000000000b1"
IMAGES = "/v1/workspaces/team/images"
BUILD = f"/v1/workspaces/team/image-builds/{BUILD_ID}"


def _definition(image: Image, env: dict[str, str] | None = None) -> dict[str, Any]:
    return image.definition(env=env or {}).model_dump(mode="json", exclude_unset=True)


def _image(*, ready: bool, python_version: str = "3.12") -> dict[str, object]:
    return {
        "id": IMAGE_ID,
        "python_version": python_version,
        "architecture": "amd64",
        "ready": ready,
        "created_at": NOW,
    }


def _build(
    status: str = "building", phase: str = "queued", attempt: int = 1, **extra: object
) -> dict[str, object]:
    return {
        "id": BUILD_ID,
        "image_id": IMAGE_ID,
        "status": status,
        "phase": phase,
        "attempt": attempt,
        "created_at": NOW,
        **extra,
    }


def _log(number: int, data: str, attempt: int = 1) -> bytes:
    record: dict[str, object] = {"id": number, "attempt": attempt, "data": data, "time": NOW}
    return json.dumps(record).encode() + b"\n"


def _serve_build(
    api: FakeApi,
    *,
    streams: list[Iterator[bytes]],
    final: dict[str, object],
    build: dict[str, object] | None = None,
) -> None:
    api.route("POST", f"{IMAGES}/resolve")(lambda _: json_reply({"image": _image(ready=False)}))
    api.route("POST", IMAGES)(
        lambda _: json_reply({"image": _image(ready=False), "build": build or _build()})
    )
    api.route("GET", f"{BUILD}/logs")(
        lambda _: (200, {"Content-Type": "application/x-ndjson"}, streams.pop(0))
    )
    api.route("GET", BUILD)(lambda _: json_reply(final))


def _serve_sources(api: FakeApi, stored: set[str]) -> None:
    @api.route("POST", "/v1/workspaces/team/sources")
    def sources(request: ApiRequest) -> Reply:
        sha: str = request.json()["sha256"]
        state: dict[str, object] = {"sha256": sha, "present": sha in stored}
        if sha not in stored:
            state["upload"] = {
                "url": f"{api.url}/upload/{sha}",
                "method": "PUT",
                "headers": {},
                "expires_at": NOW,
            }
        return json_reply(state)

    @api.route("PUT", "/upload/[0-9a-f]{64}")
    def upload(request: ApiRequest) -> Reply:
        stored.add(request.path.rsplit("/", 1)[1])
        return 200, {}, b""


@dataclass
class RecordingTerminal(Terminal):
    summaries: list[str] = field(default_factory=list)

    def step(self, name: str, summary: str = "") -> TerminalStep:
        terminal = self

        class Step(TerminalStep):
            def update(self, summary: str) -> None:
                terminal.summaries.append(summary)
                super().update(summary)

        return Step(name=name, terminal=self, summary=summary)


def test_default_image_sends_no_base_and_versions_normalize() -> None:
    assert _definition(Image()) == {"python_version": "3.12", "architecture": "amd64"}
    assert _definition(Image(python_version="python3.11"))["python_version"] == "3.11"
    assert _definition(Image(python_version="3.12.11"))["python_version"] == "3.12.11"
    arm = Image(architecture=LinuxArchitecture.Arm64)
    assert _definition(arm)["architecture"] == "arm64"


def test_definition_maps_every_authoring_option() -> None:
    image = (
        Image(
            python_version="3.11",
            commands=["echo base"],
            base_image="ghcr.io/team/worker:latest",
            base_image_creds={"GITHUB_USERNAME": "bot", "GITHUB_TOKEN": "gh-token-value"},
        )
        .add_python_packages(["httpx >= 0.27"])
        .add_commands(["python -m compileall /workspace"])
        .with_envs({"MODE": "test"})
        .with_secrets(["API_TOKEN", "API_TOKEN"])
        .build_with_gpu("A10G")
    )

    assert _definition(image) == {
        "python_version": "3.11",
        "architecture": "amd64",
        "base_image": "ghcr.io/team/worker:latest",
        "base_image_credentials": {"GITHUB_USERNAME": "bot", "GITHUB_TOKEN": "gh-token-value"},
        "steps": [
            {"kind": "pip", "args": ["httpx>=0.27"]},
            {"kind": "shell", "command": "python -m compileall /workspace"},
        ],
        "commands": ["echo base"],
        "env": {"MODE": "test"},
        "secrets": ["API_TOKEN"],
        "gpu": "A10G",
    }
    assert "gh-token-value" not in json.dumps(image.spec().model_dump(mode="json"))


def test_micromamba_images_send_the_python_release_and_their_steps(tmp_path: Path) -> None:
    environment = tmp_path / "environment.yml"
    environment.write_text(
        "channels: [conda-forge]\ndependencies:\n  - python=3.11\n  - numpy\n", encoding="utf-8"
    )
    image = Image.from_micromamba(environment).add_micromamba_packages(
        ["scipy"], channels=["bioconda"]
    )

    definition = _definition(image)

    assert definition["python_version"] == "3.11"
    assert definition["micromamba"] is True
    assert definition["steps"] == [
        {"kind": "micromamba_environment", "args": ["environment.yml", "3.11"]},
        {"kind": "micromamba", "args": ["scipy"]},
        {"kind": "micromamba", "args": ["-c bioconda"]},
    ]
    assert len(definition["context"]["sha256"]) == 64


def test_named_credentials_come_from_the_environment() -> None:
    image = Image.from_registry("ghcr.io/team/worker:latest", credentials=["GITHUB_TOKEN"])

    assert _definition(image, {"GITHUB_TOKEN": "gh-secret"})["base_image_credentials"] == {
        "GITHUB_TOKEN": "gh-secret"
    }
    with pytest.raises(ImageCredentialLookupError, match="GITHUB_TOKEN"):
        image.definition(env={})


def test_google_service_account_file_is_sent_as_json(tmp_path: Path) -> None:
    credentials_path = tmp_path / "service-account.json"
    credentials_path.write_text(
        json.dumps(
            {
                "type": "service_account",
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

    definition = _definition(image, {"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)})

    sent = definition["base_image_credentials"]["GOOGLE_APPLICATION_CREDENTIALS"]
    assert json.loads(sent)["client_email"] == "builder@example.iam.gserviceaccount.com"
    credentials_path.write_text('{"type":"authorized_user"}', encoding="utf-8")
    with pytest.raises(ValueError, match="service-account JSON"):
        image.definition(env={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials_path)})


def test_build_context_rejects_symlink_to_outside_file(tmp_path: Path) -> None:
    context = tmp_path / "context"
    context.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("must not be uploaded", encoding="utf-8")
    (context / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (context / "secret.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="build context symlinks are not allowed"):
        Image.from_dockerfile(context / "Dockerfile", context_dir=context).definition()


def test_dockerfile_images_cannot_set_a_custom_base(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")

    with pytest.raises(ValueError, match="custom base image"):
        Image(base_image="ubuntu:24.04")._with_dockerfile(tmp_path / "Dockerfile")


def test_uv_project_requires_lockfile(tmp_path: Path) -> None:
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


def test_uv_project_identity_covers_only_what_the_build_reads(tmp_path: Path) -> None:
    _write_uv_project(tmp_path)
    before = _definition(Image.from_uv(tmp_path))

    (tmp_path / "src" / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("scratch\n", encoding="utf-8")

    assert _definition(Image.from_uv(tmp_path)) == before
    assert before["steps"] == [{"kind": "uv_project", "args": ["."]}]

    (tmp_path / "uv.lock").write_text(
        (tmp_path / "uv.lock").read_text(encoding="utf-8") + "\n[[package]]\nname = 'httpx'\n",
        encoding="utf-8",
    )

    assert _definition(Image.from_uv(tmp_path))["context"] != before["context"]


def test_uv_project_includes_workspace_members_inside_the_root(tmp_path: Path) -> None:
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
        "packages/member/member.py",
        "packages/member/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
    )

    outside = tmp_path / "outside"
    _write_uv_project(outside, members=("../root/packages/member",))

    with pytest.raises(ValueError, match="outside the project root"):
        Image.from_uv(outside)


def test_a_ready_image_is_cached_without_a_build_or_upload(
    fake_api: FakeApi, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_api.route("POST", f"{IMAGES}/resolve")(lambda _: json_reply({"image": _image(ready=True)}))

    with output(enabled=True):
        result = Image().build(terminal=Terminal(default_enabled=False))

    assert result.success is True
    assert (result.image_id, result.python_version) == (IMAGE_ID, "3.12")
    assert [request.path for request in fake_api.requests] == [f"{IMAGES}/resolve"]
    (resolve,) = fake_api.requests
    assert resolve.json() == {"python_version": "3.12", "architecture": "amd64"}
    assert "python 3.12 · cached" in capsys.readouterr().err


def test_project_context_is_uploaded_once_and_sent_by_digest(
    tmp_path: Path, fake_api: FakeApi
) -> None:
    _write_uv_project(tmp_path)
    stored: set[str] = set()
    _serve_sources(fake_api, stored)
    fake_api.route("POST", f"{IMAGES}/resolve")(lambda _: json_reply({"image": _image(ready=True)}))
    image = Image.from_uv(tmp_path)

    assert image.verify().exists is True
    assert image.verify().exists is True

    (upload,) = fake_api.calls("PUT", "/upload/.*")
    sha = upload.path.rsplit("/", 1)[1]
    assert hashlib.sha256(upload.body).hexdigest() == sha
    assert sorted(zipfile.ZipFile(io.BytesIO(upload.body)).namelist()) == [
        "pyproject.toml",
        "uv.lock",
    ]
    resolves = fake_api.calls("POST", f"{IMAGES}/resolve")
    assert [request.json()["context"] for request in resolves] == [{"sha256": sha}] * 2


@pytest.mark.parametrize("success", [False, True])
def test_build_output_is_opt_in_and_uses_stderr(
    fake_api: FakeApi, capsys: pytest.CaptureFixture[str], success: bool
) -> None:
    lines = [f"build-line-{index}" for index in range(6)]
    final = (
        _build("succeeded", "finished")
        if success
        else _build("failed", "finished", failure="RUN pip install numpy exited with 1")
    )
    for enabled in (None, True, False):
        streams = [iter([_log(index + 1, line) for index, line in enumerate(lines)])]
        _serve_build(fake_api, streams=streams, final=final)
        with output(enabled=enabled) if enabled is not None else nullcontext():
            result = Image(python_packages=["numpy"]).build(
                terminal=Terminal(default_enabled=False)
            )

        assert result.success is success
        assert result.build_id == BUILD_ID
        assert result.error == ("" if success else "RUN pip install numpy exited with 1")
        captured = capsys.readouterr()
        assert captured.out == ""
        if enabled is True:
            assert all(captured.err.count(line) == 1 for line in lines)
            outcome = "python 3.12 · built" if success else "RUN pip install numpy exited with 1"
            assert outcome in captured.err
        else:
            assert captured.err == ""
    build = fake_api.calls("POST", IMAGES)[0]
    assert build.json()["python_packages"] == ["numpy"]
    assert "force" not in build.query


def test_build_summary_follows_buildkit_steps_retries_and_dropped_streams(
    fake_api: FakeApi,
) -> None:
    def dropped() -> Iterator[bytes]:
        yield _log(1, "#7 [2/4] RUN pip install numpy")
        raise StreamAborted

    def resumed() -> Iterator[bytes]:
        yield _log(2, "#3 [1/4] FROM docker.io/library/python:3.12@sha256:abc", attempt=2)

    _serve_build(
        fake_api,
        streams=[dropped(), resumed()],
        final=_build("succeeded", "finished", attempt=2),
    )
    terminal = RecordingTerminal(default_enabled=False)

    result = Image(python_packages=["numpy"]).build(terminal=terminal)

    assert result.success is True
    assert terminal.summaries == [
        "queued",
        "step 2/4 · RUN pip install numpy",
        "retry 2/2 · step 1/4 · FROM docker.io/library/python:3.12",
    ]
    logs = fake_api.calls("GET", f"{BUILD}/logs")
    assert [(r.query["after"], r.query["follow"]) for r in logs] == [
        (["0"], ["true"]),
        (["1"], ["true"]),
    ]
    (wait,) = fake_api.calls("GET", BUILD)
    assert wait.query["wait_seconds"] == ["30"]


def test_verify_reports_rejected_definitions_instead_of_raising(fake_api: FakeApi) -> None:
    fake_api.route("POST", f"{IMAGES}/resolve")(
        lambda _: error_reply("invalid_request", "base image ubuntu:nope was not found", 400)
    )

    verification = Image(base_image="ubuntu:nope").verify()
    exists, result = Image(base_image="ubuntu:nope").exists()

    assert (verification.valid, verification.exists) == (False, False)
    assert verification.reason == "base image ubuntu:nope was not found"
    assert (exists, result.success, result.error) == (
        False,
        False,
        "base image ubuntu:nope was not found",
    )


def test_verify_force_rebuild_reports_a_ready_image_missing(fake_api: FakeApi) -> None:
    building: dict[str, object] = {"image": _image(ready=True), "build": _build()}
    fake_api.route("POST", f"{IMAGES}/resolve")(lambda _: json_reply(building))

    verification = Image().verify(force_rebuild=True)

    assert (verification.image_id, verification.valid, verification.exists) == (
        IMAGE_ID,
        True,
        False,
    )
    assert verification.build_id == BUILD_ID


def test_from_id_reads_the_image_and_fails_for_an_unknown_id(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{IMAGES}/{IMAGE_ID}")(
        lambda _: json_reply(_image(ready=True, python_version="3.11"))
    )

    built = Image.from_id(IMAGE_ID).build()
    missing = Image.from_id("img_ffffffffffffffffffffffff").build()

    assert (built.success, built.image_id, built.python_version) == (True, IMAGE_ID, "3.11")
    assert (missing.success, missing.error) == (False, "no such route")
    assert fake_api.calls("POST", f"{IMAGES}.*") == []


def test_builds_on_a_joined_machine_are_unsupported(fake_api: FakeApi) -> None:
    with pytest.raises(UnsupportedFeatureError, match="machine"):
        Image().build(machine="gpu-box")

    assert fake_api.requests == []


def test_from_id_of_an_image_being_converted_waits_for_its_build(fake_api: FakeApi) -> None:
    fake_api.route("GET", f"{IMAGES}/{IMAGE_ID}")(lambda _: json_reply(_image(ready=False)))
    fake_api.route("POST", f"{IMAGES}/{IMAGE_ID}/prepare")(
        lambda _: json_reply({"image": _image(ready=False), "build": _build()})
    )
    fake_api.route("GET", f"{BUILD}/logs")(
        lambda _: (
            200,
            {"Content-Type": "application/x-ndjson"},
            iter([_log(1, "converted 4 layers")]),
        )
    )
    fake_api.route("GET", BUILD)(lambda _: json_reply(_build("succeeded", "finished")))
    terminal = RecordingTerminal(default_enabled=False)

    built = Image.from_id(IMAGE_ID).build(terminal=terminal)

    assert (built.success, built.image_id, built.build_id) == (True, IMAGE_ID, BUILD_ID)
    assert terminal.summaries[0] == "queued"
    assert len(fake_api.calls("POST", f"{IMAGES}/{IMAGE_ID}/prepare")) == 1


def test_from_id_reports_a_conversion_that_cannot_run_as_the_build_failure(
    fake_api: FakeApi,
) -> None:
    fake_api.route("GET", f"{IMAGES}/{IMAGE_ID}")(lambda _: json_reply(_image(ready=False)))
    fake_api.route("POST", f"{IMAGES}/{IMAGE_ID}/prepare")(
        lambda _: error_reply(
            "invalid_request", "the image cannot be converted: layer 0 is foreign", 400
        )
    )

    result = Image.from_id(IMAGE_ID).build()

    assert (result.success, result.image_id) == (False, IMAGE_ID)
    assert result.error == "the image cannot be converted: layer 0 is foreign"

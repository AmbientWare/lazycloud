from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import JsonValue, TypeAdapter
from shared.app_identity import IMAGE_BUILD_CONTEXT_BUCKET
from shared.http.images import (
    BuildImageRequest,
    BuildImageResponse,
    BuildStep,
    VerifyImageBuildRequest,
    VerifyImageBuildResponse,
)
from shared.image_building import (
    DEFAULT_IMAGE_BASE,
    fingerprint_build_context,
    load_requirements_file,
    sanitize_python_packages,
)
from shared.image_building.authoring import (
    ImageBuildStep,
    ImageBuildStepKind,
    ImageSpec,
    LinuxArchitecture,
    PythonVersion,
)
from shared.image_building.credentials import (
    ImageCredentialEnvVar,
    ImageCredentialInput,
    credential_key_names,
    dedupe_names,
    resolve_registry_credentials,
)
from typing_extensions import Self

from lazycloud.terminal import ProgressCallback, Terminal, TerminalStep

_DOCKER_APT_DISTRIBUTION = (
    "set -eu; . /etc/os-release; "
    'case "$ID" in debian|ubuntu) ;; '
    '*) echo "Docker installation supports only Debian and Ubuntu base images; got $ID" '
    ">&2; exit 64 ;; esac; "
)

_DOCKER_INSTALL_COMMANDS = (
    (
        "apt-get update && DEBIAN_FRONTEND=noninteractive "
        "apt-get install -y ca-certificates curl gnupg"
    ),
    (
        _DOCKER_APT_DISTRIBUTION
        + "install -m 0755 -d /etc/apt/keyrings; "
        + 'curl -fsSL "https://download.docker.com/linux/$ID/gpg" '
        + "| gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg; "
        + "chmod a+r /etc/apt/keyrings/docker.gpg"
    ),
    (
        _DOCKER_APT_DISTRIBUTION
        + 'test -n "${VERSION_CODENAME:-}" || '
        + '(echo "Docker installation requires VERSION_CODENAME in /etc/os-release" '
        + ">&2; exit 64); "
        + "printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] %s %s stable\\n' "
        + '"$(dpkg --print-architecture)" "https://download.docker.com/linux/$ID" '
        + '"$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list'
    ),
    (
        "apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io "
        "docker-buildx-plugin docker-compose-plugin"
    ),
    (
        "test -x /usr/local/bin/docker-compose || "
        "ln -sf /usr/libexec/docker/cli-plugins/docker-compose "
        "/usr/local/bin/docker-compose"
    ),
    "docker --version && docker compose version",
    "apt-get clean && rm -rf /var/lib/apt/lists/*",
)


@dataclass(frozen=True)
class ImageBuildResult:
    success: bool
    image_id: str = ""
    python_version: str = ""
    build_id: str = ""
    error: str = ""
    responses: tuple[BuildImageResponse, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ImageBuildContext:
    path: str
    digest: str
    data: bytes
    files: tuple[str, ...] = field(default_factory=tuple)


class ImageBuildClient(Protocol):
    def build_image(self, request: BuildImageRequest) -> Iterator[BuildImageResponse]: ...


@runtime_checkable
class ImageVerifyClient(Protocol):
    def verify_image_build(self, request: VerifyImageBuildRequest) -> VerifyImageBuildResponse: ...


@runtime_checkable
class ImageContextUploadResult(Protocol):
    @property
    def object_id(self) -> str: ...


class ImageContextUploadClient(Protocol):
    def upload_bytes(
        self,
        data: bytes,
        *,
        name: str,
        bucket: str = "default",
        overwrite: bool = False,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        progress: ProgressCallback | None = None,
    ) -> ImageContextUploadResult | Mapping[str, JsonValue]: ...


@dataclass(init=False)
class Image:
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    base: str = DEFAULT_IMAGE_BASE
    python_version: str = "3.12"
    packages: tuple[str, ...] = field(default_factory=tuple)
    commands: tuple[str, ...] = field(default_factory=tuple)
    build_steps: tuple[ImageBuildStep, ...] = field(default_factory=tuple)
    env_vars: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    workdir_path: str = "/workspace"
    dockerfile_content: str | None = None
    context_path: str | None = None
    context_digest: str | None = None
    credential_keys: tuple[str, ...] = field(default_factory=tuple)
    credential_values: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    secrets: tuple[str, ...] = field(default_factory=tuple)
    gpu_hint: str | None = None
    explicit_image_id: str | None = None
    ignore_python: bool = False
    include_files_patterns: tuple[str, ...] = field(default_factory=tuple)
    context_object_id: str | None = None
    _base_image_explicit: bool = field(default=False, init=False, repr=False)

    def __init__(
        self,
        python_version: str = "python3.12",
        python_packages: Sequence[str] | str = (),
        commands: Sequence[str] = (),
        base_image: str | None = None,
        base_image_creds: ImageCredentialInput = None,
        env_vars: Mapping[str, str] | Sequence[str] | str | None = None,
        image_id: str | None = None,
        architecture: LinuxArchitecture | str = LinuxArchitecture.Amd64,
    ) -> None:
        self.architecture = LinuxArchitecture(architecture)
        self.python_version = _normalize_python_version(str(python_version))
        self.base = base_image or _default_image_base(self.python_version)
        self._base_image_explicit = base_image is not None
        self.packages = tuple(_constructor_python_packages(python_packages))
        self.commands = tuple(str(command) for command in commands)
        self.build_steps = ()
        self.env_vars = ()
        self.workdir_path = "/workspace"
        self.dockerfile_content = None
        self.context_path = None
        self.context_digest = None
        self.credential_keys = tuple(credential_key_names(base_image_creds))
        self.credential_values = _credential_values(base_image_creds)
        self.secrets = ()
        self.gpu_hint = None
        self.explicit_image_id = image_id
        self.ignore_python = False
        self.include_files_patterns = ()
        self.context_object_id = None
        if env_vars is not None:
            self.with_envs(env_vars)

    @classmethod
    def from_registry(cls, image_uri: str, credentials: ImageCredentialInput = None) -> Self:
        return cls(base_image=image_uri, base_image_creds=credentials)

    @classmethod
    def from_id(cls, image_id: str) -> Self:
        return cls(image_id=image_id)

    @classmethod
    def from_dockerfile(cls, path: str | Path, context_dir: str | Path | None = None) -> Self:
        return cls()._with_dockerfile(path, context_dir=context_dir)

    def micromamba(self) -> Self:
        if not self.python_version.startswith("micromamba"):
            self.python_version = _micromamba_python_version(self.python_version)
        return self

    def add_commands(self, commands: Sequence[str]) -> Self:
        self.build_steps = (
            *self.build_steps,
            *(
                ImageBuildStep(kind=ImageBuildStepKind.Shell, command=command)
                for command in commands
            ),
        )
        return self

    def add_python_packages(self, packages: Sequence[str] | str | Path) -> Self:
        values = _packages_or_requirements(packages, missing_file_error=True)
        self.build_steps = (
            *self.build_steps,
            *(
                ImageBuildStep(kind=ImageBuildStepKind.Pip, args=[package])
                for package in sanitize_python_packages(values)
            ),
        )
        return self

    def add_uv_project(
        self,
        path: str | Path = ".",
        *,
        extras: Sequence[str] = (),
    ) -> Self:
        project_path = Path(path).expanduser().resolve()
        pyproject_path = project_path / "pyproject.toml"
        lock_path = project_path / "uv.lock"
        if not pyproject_path.is_file():
            msg = f"uv project must contain pyproject.toml: {project_path}"
            raise ValueError(msg)
        if not lock_path.is_file():
            msg = f"uv project must contain uv.lock: {project_path}"
            raise ValueError(msg)
        if self.context_path is not None:
            existing_context = Path(self.context_path).expanduser().resolve()
            if existing_context != project_path:
                msg = "uv project path must match the existing image build context"
                raise ValueError(msg)

        self.context_path = str(project_path)
        self.context_digest = fingerprint_build_context(project_path)
        patterns = ["pyproject.toml", "uv.lock"]
        if (project_path / ".python-version").is_file():
            patterns.append(".python-version")
        self.include_files_patterns = tuple(
            dict.fromkeys((*self.include_files_patterns, *patterns))
        )
        self.build_steps = (
            *self.build_steps,
            ImageBuildStep(
                kind=ImageBuildStepKind.UvProject,
                args=[".", *sanitize_python_packages(extras)],
            ),
        )
        return self

    def add_micromamba_packages(
        self,
        packages: Sequence[str] | str | Path,
        *,
        channels: Sequence[str] = (),
    ) -> Self:
        if not self.python_version.startswith("micromamba"):
            msg = "micromamba must be enabled before adding micromamba packages"
            raise ValueError(msg)
        values = [
            *sanitize_python_packages(_packages_or_requirements(packages, missing_file_error=True))
        ]
        values.extend(f"-c {channel}" for channel in channels)
        self.build_steps = (
            *self.build_steps,
            *(
                ImageBuildStep(kind=ImageBuildStepKind.Micromamba, args=[package])
                for package in values
            ),
        )
        return self

    def with_envs(
        self,
        env_vars: Mapping[str, str] | Sequence[str] | str,
        *,
        clear: bool = False,
    ) -> Self:
        values = tuple(_env_items(env_vars))
        self.env_vars = values if clear else (*self.env_vars, *values)
        return self

    def _with_dockerfile(self, path: str | Path, *, context_dir: str | Path | None = None) -> Self:
        if self.base != DEFAULT_IMAGE_BASE:
            msg = "dockerfile builds cannot also set a custom base image"
            raise ValueError(msg)
        dockerfile_path = Path(path)
        context = Path(context_dir) if context_dir is not None else dockerfile_path.parent
        self.dockerfile_content = dockerfile_path.read_text(encoding="utf-8")
        self.context_path = str(context)
        self.context_digest = fingerprint_build_context(context)
        return self

    def add_local_path(self, pattern: str = "*") -> Self:
        path = Path(pattern).as_posix()
        if path == ".":
            path = "*"
        self.context_path = self.context_path or "."
        self.context_digest = fingerprint_build_context(self.context_path)
        self.include_files_patterns = (*self.include_files_patterns, path)
        return self

    def with_secrets(self, secrets: Sequence[str]) -> Self:
        self.secrets = (*self.secrets, *secrets)
        return self

    def build_with_gpu(self, hint: str) -> Self:
        self.gpu_hint = hint
        return self

    def add_python_version(self, python_version: str) -> Self:
        normalized = _normalize_python_version(python_version)
        if not self._base_image_explicit:
            self.base = _default_image_base(normalized)
        self.python_version = normalized
        self.ignore_python = False
        return self

    def with_docker(self) -> Self:
        return self.add_commands(_DOCKER_INSTALL_COMMANDS)

    def get_credentials_from_env(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        resolved = {key: value for key, value in self.credential_values if key and value}
        unresolved_keys = [key for key in dedupe_names(self.credential_keys) if key not in resolved]
        if unresolved_keys:
            resolved.update(resolve_registry_credentials(unresolved_keys, env=env))
        return _registry_credentials_for_transport(resolved)

    def _build_request(self, *, env: Mapping[str, str] | None = None) -> BuildImageRequest:
        spec = self.spec()
        return BuildImageRequest(
            architecture=spec.architecture,
            python_version=spec.python_version,
            python_packages=list(spec.packages),
            commands=list(spec.commands),
            existing_image_uri=spec.base,
            existing_image_creds=self.get_credentials_from_env(env),
            build_steps=[_http_build_step(step) for step in spec.build_steps],
            env_vars=[f"{key}={value}" for key, value in spec.env.items()],
            dockerfile=spec.dockerfile or "",
            build_ctx_object=self._build_context_object(),
            build_ctx_digest=spec.context_digest or "",
            secrets=list(spec.secrets),
            gpu=spec.gpu or "",
            ignore_python=spec.ignore_python,
        )

    def _verify_request(
        self,
        *,
        force_rebuild: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> VerifyImageBuildRequest:
        spec = self.spec()
        return VerifyImageBuildRequest(
            architecture=spec.architecture,
            python_version=spec.python_version,
            python_packages=list(spec.packages),
            commands=list(spec.commands),
            force_rebuild=force_rebuild,
            existing_image_uri=spec.base,
            existing_image_creds=self.get_credentials_from_env(env),
            build_steps=[_http_build_step(step) for step in spec.build_steps],
            env_vars=[f"{key}={value}" for key, value in spec.env.items()],
            dockerfile=spec.dockerfile or "",
            build_ctx_object=self._build_context_object(),
            build_ctx_digest=spec.context_digest or "",
            secrets=list(spec.secrets),
            gpu=spec.gpu or "",
            ignore_python=spec.ignore_python,
            image_id=spec.image_id,
        )

    def verify(
        self,
        client: ImageVerifyClient,
        *,
        force_rebuild: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> VerifyImageBuildResponse:
        return client.verify_image_build(self._verify_request(force_rebuild=force_rebuild, env=env))

    def exists(
        self,
        client: ImageVerifyClient,
        *,
        force_rebuild: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> tuple[bool, ImageBuildResult]:
        response = self.verify(client, force_rebuild=force_rebuild, env=env)
        return (
            response.exists,
            ImageBuildResult(
                success=response.exists,
                image_id=response.image_id,
                python_version=self.spec().python_version,
                build_id=response.build_id,
                error="" if response.valid else response.reason,
            ),
        )

    def _build_stream(
        self,
        client: ImageBuildClient,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Iterator[BuildImageResponse]:
        if self.explicit_image_id:
            yield BuildImageResponse(
                image_id=self.explicit_image_id,
                done=True,
                success=True,
                python_version=self.spec().python_version,
            )
            return
        yield from client.build_image(self._build_request(env=env))

    def build(
        self,
        client: ImageBuildClient,
        *,
        terminal: Terminal | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ImageBuildResult:
        step = terminal.step("Image", "preparing") if terminal is not None else None
        if self.explicit_image_id:
            if step is not None:
                step.done(f"{self.explicit_image_id} · pinned")
            return ImageBuildResult(
                success=True,
                image_id=self.explicit_image_id,
                python_version=self.spec().python_version,
            )

        verify_client = _image_verify_client(client)
        if verify_client is not None:
            exists, exists_result = self.exists(verify_client, env=env)
            if exists:
                result = ImageBuildResult(
                    success=True,
                    image_id=exists_result.image_id,
                    python_version=exists_result.python_version,
                    build_id=exists_result.build_id,
                )
                if step is not None:
                    step.done(f"python {exists_result.python_version} · cached")
                return result
            if exists_result.error:
                if step is not None:
                    step.fail(exists_result.error)
                return exists_result

        responses: list[BuildImageResponse] = []
        last_response: BuildImageResponse | None = None
        for response in self._build_stream(client, env=env):
            responses.append(response)
            if step is not None:
                _write_build_response(step, response)
            if response.done:
                last_response = response
                break

        if last_response is None:
            if step is not None:
                step.fail("the build stream ended before the build finished")
            return ImageBuildResult(
                success=False,
                error="image build produced no terminal response",
                responses=tuple(responses),
            )

        result = ImageBuildResult(
            success=last_response.success,
            image_id=last_response.image_id,
            python_version=last_response.python_version,
            build_id=last_response.build_id,
            error=_response_error(last_response),
            responses=tuple(responses),
        )
        return result

    def spec(self) -> ImageSpec:
        return ImageSpec(
            architecture=self.architecture,
            base=self.base,
            python_version=_normalize_python_version(self.python_version),
            packages=list(self.packages),
            commands=list(self.commands),
            build_steps=list(self.build_steps),
            env=dict(self.env_vars),
            workdir=self.workdir_path,
            dockerfile=self.dockerfile_content,
            context_path=self.context_path,
            context_digest=self.context_digest,
            context_object_id=self.context_object_id,
            include_files_patterns=list(self.include_files_patterns),
            credential_keys=dedupe_names(self.credential_keys),
            secrets=dedupe_names(self.secrets),
            gpu=self.gpu_hint,
            image_id=self.explicit_image_id,
            ignore_python=self.ignore_python,
        )

    def _context_archive(self) -> ImageBuildContext:
        context = Path(self.context_path or ".").expanduser().resolve()
        files = _context_files(context, self.include_files_patterns)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative_path in files:
                source = context / relative_path
                _assert_safe_context_source(context, source)
                info = zipfile.ZipInfo(str(relative_path).replace("\\", "/"))
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (source.stat().st_mode & 0o777) << 16
                with source.open("rb") as handle:
                    archive.writestr(info, handle.read())
        data = buffer.getvalue()
        return ImageBuildContext(
            path=str(context),
            digest=hashlib.sha256(data).hexdigest(),
            data=data,
            files=tuple(str(path).replace("\\", "/") for path in files),
        )

    def _sync_context(
        self,
        client: ImageContextUploadClient,
        *,
        bucket: str = IMAGE_BUILD_CONTEXT_BUCKET,
        name: str | None = None,
        overwrite: bool = True,
    ) -> Self:
        context = self._context_archive()
        if not context.files and self.dockerfile_content is None:
            return self
        object_name = name or f"image-context-{context.digest}.zip"
        uploaded = client.upload_bytes(
            context.data,
            name=object_name,
            bucket=bucket,
            overwrite=overwrite,
            content_type="application/zip",
            metadata={"digest": context.digest, "path": context.path},
        )
        object_id = uploaded.object_id if isinstance(uploaded, ImageContextUploadResult) else ""
        if not object_id and isinstance(uploaded, Mapping):
            object_id = str(uploaded.get("object_id", ""))
        if not object_id:
            msg = "context upload did not return an object_id"
            raise ValueError(msg)
        self.context_digest = context.digest
        self.context_object_id = object_id
        return self

    def _build_context_object(self) -> str:
        return self.context_object_id or ""


def _image_verify_client(client: ImageBuildClient) -> ImageVerifyClient | None:
    if isinstance(client, ImageVerifyClient):
        return client
    return None


def _credential_values(
    credentials: ImageCredentialInput,
) -> tuple[tuple[str, str], ...]:
    if isinstance(credentials, Mapping):
        return tuple((str(key), str(value)) for key, value in credentials.items() if value)
    return ()


def _registry_credentials_for_transport(credentials: dict[str, str]) -> dict[str, str]:
    result = dict(credentials)
    key = ImageCredentialEnvVar.GoogleApplicationCredentials.value
    value = result.get(key, "").strip()
    if not value:
        return result
    if value.startswith("{"):
        serialized = value
    else:
        path = Path(value).expanduser()
        if not path.is_file():
            raise ValueError(f"GOOGLE_APPLICATION_CREDENTIALS file not found: {path}")
        serialized = path.read_text(encoding="utf-8")
    try:
        payload = TypeAdapter(dict[str, JsonValue]).validate_json(serialized)
    except ValueError as exc:
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS must contain valid JSON") from exc
    client_email = payload.get("client_email")
    private_key = payload.get("private_key")
    if (
        payload.get("type") != "service_account"
        or not isinstance(client_email, str)
        or not client_email
        or not isinstance(private_key, str)
        or not private_key
    ):
        raise ValueError("GOOGLE_APPLICATION_CREDENTIALS is not service-account JSON")
    result[key] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return result


def _sequence(values: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        return (values,)
    return tuple(values)


def _constructor_python_packages(values: Sequence[str] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        return tuple(load_requirements_file(values))
    return tuple(sanitize_python_packages(values))


def _packages_or_requirements(
    values: Sequence[str] | str | Path,
    *,
    missing_file_error: bool = False,
) -> tuple[str, ...]:
    if isinstance(values, str | Path):
        path = Path(values)
        if path.exists():
            return tuple(load_requirements_file(path))
        if missing_file_error:
            msg = (
                f"Could not find valid requirements.txt file at {values}. "
                "Libraries must be specified as a list of valid package names "
                "or a path to a requirements.txt file."
            )
            raise ValueError(msg)
        return (str(values),)
    return tuple(values)


def _env_items(
    values: Mapping[str, str] | Sequence[str] | str,
) -> list[tuple[str, str]]:
    if isinstance(values, Mapping):
        entries = [f"{key}={value}" for key, value in values.items()]
    else:
        entries = _sequence(values)
    result: list[tuple[str, str]] = []
    for entry in entries:
        key, separator, value = entry.partition("=")
        if not separator or not key:
            msg = f"environment variable must use KEY=VALUE format: {entry}"
            raise ValueError(msg)
        if not value:
            msg = f"environment variable value cannot be empty: {entry}"
            raise ValueError(msg)
        if "=" in value:
            msg = f"environment variable value cannot contain '=': {entry}"
            raise ValueError(msg)
        result.append((key, value))
    return result


def _normalize_python_version(python_version: str) -> str:
    value = python_version.strip()
    prefix = ""
    if value.startswith("micromamba"):
        prefix = "micromamba"
        value = value.removeprefix(prefix)
    else:
        value = value.removeprefix("python")
    try:
        normalized = PythonVersion(value).value
    except ValueError as exc:
        supported = ", ".join(version.value for version in PythonVersion)
        msg = f"Python version must be one of {supported}; received {python_version!r}"
        raise ValueError(msg) from exc
    return f"{prefix}{normalized}"


def _default_image_base(python_version: str) -> str:
    if python_version in {version.value for version in PythonVersion}:
        return f"python:{python_version}-slim"
    return DEFAULT_IMAGE_BASE


def _micromamba_python_version(python_version: str) -> str:
    value = _normalize_python_version(python_version)
    if value.startswith("micromamba"):
        return value
    return f"micromamba{value}"


def _http_build_step(step: ImageBuildStep) -> BuildStep:
    command = step.command or " ".join(step.args)
    return BuildStep(type=step.kind.value, command=command)


def _context_files(context: Path, patterns: tuple[str, ...]) -> list[Path]:
    if not context.exists():
        msg = f"build context does not exist: {context}"
        raise FileNotFoundError(msg)
    selected_patterns = patterns or ("**/*",)
    files: set[Path] = set()
    for pattern in selected_patterns:
        for source in _matched_sources(context, pattern):
            _assert_safe_context_source(context, source)
            if source.is_dir():
                for path in source.rglob("*"):
                    _assert_safe_context_source(context, path)
                    if path.is_file() and not _ignored_context_path(path):
                        files.add(path.relative_to(context))
            elif source.is_file() and not _ignored_context_path(source):
                files.add(source.relative_to(context))
    return sorted(files)


def _matched_sources(context: Path, pattern: str) -> Iterator[Path]:
    pattern_path = Path(pattern)
    if pattern_path.is_absolute() or ".." in pattern_path.parts:
        return iter(())
    candidate = context / pattern_path
    if candidate.exists() or candidate.is_symlink():
        return iter((candidate,))
    return (
        path
        for path in context.rglob("*")
        if pattern == "**/*" or fnmatch.fnmatch(str(path.relative_to(context)), pattern)
    )


def _assert_safe_context_source(context: Path, source: Path) -> None:
    try:
        relative = source.relative_to(context)
    except ValueError as exc:
        raise ValueError(f"unsafe build context path: {source}") from exc
    current = context
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"build context symlinks are not allowed: {relative}")


def _ignored_context_path(path: Path) -> bool:
    parts = set(path.parts)
    ignored = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    return bool(parts.intersection(ignored))


def _response_error(response: BuildImageResponse) -> str:
    if response.success:
        return ""
    return response.error or response.msg.strip()


def _write_build_response(step: TerminalStep, response: BuildImageResponse) -> None:
    if response.warning and response.msg:
        step.log(f"warning: {response.msg.rstrip()}")
        return
    if response.msg and not response.done:
        for line in response.msg.replace("\r", "\n").splitlines():
            if line.strip():
                step.log(line)
                step.update(_build_summary(line, step.summary))
        return
    if response.done and response.success:
        step.done(f"python {response.python_version} · built".strip())
        return
    if response.done and not response.success:
        step.fail(_response_error(response))


_BUILD_STEP = re.compile(r"^STEP (\d+)/(\d+): (.*)$")
_BUILD_STAGES = (
    ("submitting build container request", "submitting"),
    ("container request queued", "queued"),
    ("build container execution started", "starting builder"),
    ("image build worker request accepted", "building"),
    ("image archive ready", "archived"),
)


def _build_summary(line: str, current: str) -> str:
    """The one-line build summary a log line implies, or the current one."""
    text = line.strip()
    matched = _BUILD_STEP.match(text)
    if matched is not None:
        instruction = matched.group(3).split("@", 1)[0]
        return f"step {matched.group(1)}/{matched.group(2)} · {instruction[:48]}"
    lower = text.lower()
    if lower.startswith("archive progress:"):
        return f"archiving {text.split(':', 1)[1].strip()}"
    if lower.startswith("cache key:"):
        return "planning"
    for prefix, summary in _BUILD_STAGES:
        if lower.startswith(prefix):
            return summary
    return current

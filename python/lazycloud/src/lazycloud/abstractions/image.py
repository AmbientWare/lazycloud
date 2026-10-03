from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue, TypeAdapter, ValidationError
from typing_extensions import Self

from lazycloud._shared.image_building import (
    fingerprint_build_context,
    fingerprint_files,
    load_requirements_file,
    sanitize_python_packages,
)
from lazycloud._shared.image_building.authoring import (
    PROJECT_BUILD_STEP_KINDS,
    ImageBuildStep,
    ImageBuildStepKind,
    ImageSpec,
    LinuxArchitecture,
)
from lazycloud._shared.image_building.credentials import (
    ImageCredentialEnvVar,
    ImageCredentialInput,
    credential_key_names,
    dedupe_names,
    resolve_registry_credentials,
)
from lazycloud._shared.image_building.python import normalize_python_version

# Declaring an image loads no API client; image_build makes the API calls.
if TYPE_CHECKING:
    from lazycloud.abstractions.image_project import ImageProject
    from lazycloud.clients.api import ApiClient
    from lazycloud.contracts.api import ImageDefinition
    from lazycloud.terminal import Terminal

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


@dataclass(frozen=True)
class ImageVerification:
    """Whether the platform accepts an image definition and has its image ready."""

    image_id: str
    valid: bool
    exists: bool
    build_id: str = ""
    reason: str = ""
    python_version: str = ""


@dataclass(frozen=True)
class ImageBuildContext:
    path: str
    digest: str
    data: bytes
    files: tuple[str, ...] = field(default_factory=tuple)


@dataclass(init=False)
class Image:
    architecture: LinuxArchitecture = LinuxArchitecture.Amd64
    base: str | None = None
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
        self.python_version = normalize_python_version(str(python_version))
        self.base = base_image or None
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
        if env_vars is not None:
            self.with_envs(env_vars)

    @classmethod
    def from_registry(
        cls,
        image_uri: str,
        credentials: ImageCredentialInput = None,
        *,
        python_version: str = "3.12",
    ) -> Self:
        return cls(
            base_image=image_uri, base_image_creds=credentials, python_version=python_version
        )

    @classmethod
    def from_id(cls, image_id: str) -> Self:
        return cls(image_id=image_id)

    @classmethod
    def from_dockerfile(cls, path: str | Path, context_dir: str | Path | None = None) -> Self:
        return cls()._with_dockerfile(path, context_dir=context_dir)

    @classmethod
    def from_uv(
        cls,
        path: str | Path = ".",
        *,
        python_version: str | None = None,
        extras: Sequence[str] = (),
        groups: Sequence[str] = (),
        base_image: str | None = None,
        base_image_creds: ImageCredentialInput = None,
        architecture: LinuxArchitecture | str = LinuxArchitecture.Amd64,
    ) -> Self:
        from lazycloud.abstractions.image_project import load_python_project

        return cls._from_project(
            load_python_project(
                path,
                kind=ImageBuildStepKind.UvProject,
                python_version=python_version,
                extras=extras,
                groups=groups,
            ),
            base_image=base_image,
            base_image_creds=base_image_creds,
            architecture=architecture,
        )

    @classmethod
    def from_poetry(
        cls,
        path: str | Path = ".",
        *,
        python_version: str | None = None,
        extras: Sequence[str] = (),
        groups: Sequence[str] = (),
        base_image: str | None = None,
        base_image_creds: ImageCredentialInput = None,
        architecture: LinuxArchitecture | str = LinuxArchitecture.Amd64,
    ) -> Self:
        from lazycloud.abstractions.image_project import load_python_project

        return cls._from_project(
            load_python_project(
                path,
                kind=ImageBuildStepKind.PoetryProject,
                python_version=python_version,
                extras=extras,
                groups=groups,
            ),
            base_image=base_image,
            base_image_creds=base_image_creds,
            architecture=architecture,
        )

    @classmethod
    def from_pyproject(
        cls,
        path: str | Path = ".",
        *,
        python_version: str | None = None,
        extras: Sequence[str] = (),
        groups: Sequence[str] = (),
        base_image: str | None = None,
        base_image_creds: ImageCredentialInput = None,
        architecture: LinuxArchitecture | str = LinuxArchitecture.Amd64,
    ) -> Self:
        from lazycloud.abstractions.image_project import load_python_project

        return cls._from_project(
            load_python_project(
                path,
                kind=ImageBuildStepKind.Pyproject,
                python_version=python_version,
                extras=extras,
                groups=groups,
            ),
            base_image=base_image,
            base_image_creds=base_image_creds,
            architecture=architecture,
        )

    @classmethod
    def from_micromamba(
        cls,
        path: str | Path = "environment.yml",
        *,
        python_version: str | None = None,
        base_image: str | None = None,
        base_image_creds: ImageCredentialInput = None,
        architecture: LinuxArchitecture | str = LinuxArchitecture.Amd64,
    ) -> Self:
        from lazycloud.abstractions.image_project import load_conda_environment

        return cls._from_project(
            load_conda_environment(path, python_version=python_version),
            base_image=base_image,
            base_image_creds=base_image_creds,
            architecture=architecture,
        )

    @classmethod
    def _from_project(
        cls,
        project: ImageProject,
        *,
        base_image: str | None,
        base_image_creds: ImageCredentialInput,
        architecture: LinuxArchitecture | str,
    ) -> Self:
        from lazycloud.abstractions.image_project import project_context_files

        image = cls(
            python_version=project.python_version,
            base_image=base_image,
            base_image_creds=base_image_creds,
            architecture=architecture,
        )
        image.context_path = str(project.root)
        image.context_digest = fingerprint_files(
            project.root, project_context_files(project.root, project.context_entries)
        )
        image.include_files_patterns = project.context_entries
        image.build_steps = (project.step,)
        return image

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
        if self.base is not None:
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

    def with_docker(self) -> Self:
        return self.add_commands(_DOCKER_INSTALL_COMMANDS)

    def get_credentials_from_env(self, env: Mapping[str, str] | None = None) -> dict[str, str]:
        resolved = {key: value for key, value in self.credential_values if key and value}
        unresolved_keys = [key for key in dedupe_names(self.credential_keys) if key not in resolved]
        if unresolved_keys:
            resolved.update(resolve_registry_credentials(unresolved_keys, env=env))
        return _registry_credentials_for_transport(resolved)

    def definition(
        self,
        env: Mapping[str, str] | None = None,
        context_sha256: str | None = None,
    ) -> ImageDefinition:
        """The API definition of this image.

        Credentials named in `base_image_creds` come from `env`, or from the
        process environment when it is None. Without `context_sha256`, an image
        with a build context uses the digest of its local archive.
        """
        release = normalize_python_version(self.python_version)
        fields: dict[str, Any] = {
            "python_version": release.removeprefix("micromamba"),
            "architecture": self.architecture.value,
        }
        if release.startswith("micromamba"):
            fields["micromamba"] = True
        if self.base is not None:
            fields["base_image"] = self.base
        credentials = self.get_credentials_from_env(env)
        if credentials:
            fields["base_image_credentials"] = credentials
        if self.packages:
            fields["python_packages"] = list(self.packages)
        if self.build_steps:
            fields["steps"] = [_definition_step(step) for step in self.build_steps]
        if self.commands:
            fields["commands"] = list(self.commands)
        if self.env_vars:
            fields["env"] = dict(self.env_vars)
        if self.dockerfile_content is not None:
            fields["dockerfile"] = self.dockerfile_content
        if self._has_context():
            fields["context"] = {"sha256": context_sha256 or self._context_archive().digest}
        secrets = dedupe_names(self.secrets)
        if secrets:
            fields["secrets"] = secrets
        if self.gpu_hint:
            fields["gpu"] = self.gpu_hint
        from lazycloud.contracts.api import ImageDefinition

        try:
            return ImageDefinition.model_validate(fields)
        except ValidationError as exc:
            # Inputs stay out of the message because they can hold credentials.
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False)
            )
            raise ValueError(f"invalid image definition: {problems}") from exc

    def verify(
        self,
        client: ApiClient | None = None,
        *,
        force_rebuild: bool = False,
        env: Mapping[str, str] | None = None,
        workspace: str | None = None,
    ) -> ImageVerification:
        """Resolve the image without building it.

        A definition the platform rejects returns `valid=False` with the reason.
        `force_rebuild` reports a ready image as missing.
        """
        from lazycloud.abstractions.image_build import session, verify_definition, verify_id

        api, selected = session(client, workspace)
        if self.explicit_image_id:
            return verify_id(api, selected, self.explicit_image_id)
        definition = self._prepared_definition(api, selected, env)
        return verify_definition(api, selected, definition, force_rebuild=force_rebuild)

    def exists(
        self,
        client: ApiClient | None = None,
        *,
        force_rebuild: bool = False,
        env: Mapping[str, str] | None = None,
        workspace: str | None = None,
    ) -> tuple[bool, ImageBuildResult]:
        verification = self.verify(
            client, force_rebuild=force_rebuild, env=env, workspace=workspace
        )
        from lazycloud.abstractions.image_build import verification_result

        return verification.exists, verification_result(self, verification)

    def build(
        self,
        client: ApiClient | None = None,
        *,
        terminal: Terminal | None = None,
        env: Mapping[str, str] | None = None,
        machine: str = "",
        workspace: str | None = None,
    ) -> ImageBuildResult:
        """Make the image ready, building it unless it already is."""
        from lazycloud.abstractions.image_build import ImageBuildOperation, session

        api, selected = session(client, workspace)
        with ImageBuildOperation(
            self, api, selected, terminal=terminal, env=env, machine=machine
        ) as operation:
            operation.verify()
            return operation.finish()

    def spec(self) -> ImageSpec:
        return ImageSpec(
            architecture=self.architecture,
            base=self.base or "",
            python_version=normalize_python_version(self.python_version),
            packages=list(self.packages),
            commands=list(self.commands),
            build_steps=list(self.build_steps),
            env=dict(self.env_vars),
            workdir=self.workdir_path,
            dockerfile=self.dockerfile_content,
            context_path=self.context_path,
            context_digest=self.context_digest,
            include_files_patterns=list(self.include_files_patterns),
            credential_keys=dedupe_names(self.credential_keys),
            secrets=dedupe_names(self.secrets),
            gpu=self.gpu_hint,
            image_id=self.explicit_image_id,
            ignore_python=self.ignore_python,
        )

    def _has_context(self) -> bool:
        return self.context_path is not None or self.dockerfile_content is not None

    def _context_archive(self) -> ImageBuildContext:
        from lazycloud.abstractions.image_project import project_context_files

        context = Path(self.context_path or ".").expanduser().resolve()
        if any(step.kind in PROJECT_BUILD_STEP_KINDS for step in self.build_steps):
            files = project_context_files(context, self.include_files_patterns)
        else:
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

    def _prepared_definition(
        self, client: ApiClient, workspace: str, env: Mapping[str, str] | None
    ) -> ImageDefinition:
        """The definition, after storing its build context in the workspace."""
        if not self._has_context():
            return self.definition(env)
        context = self._context_archive()
        client.store_source(workspace, context.digest, context.data)
        return self.definition(env, context_sha256=context.digest)


def _definition_step(step: ImageBuildStep) -> dict[str, Any]:
    from lazycloud.contracts.api import ImageStepKind

    kinds = {
        ImageBuildStepKind.Shell: ImageStepKind.shell,
        ImageBuildStepKind.Pip: ImageStepKind.pip,
        ImageBuildStepKind.Micromamba: ImageStepKind.micromamba,
        ImageBuildStepKind.UvProject: ImageStepKind.uv_project,
        ImageBuildStepKind.PoetryProject: ImageStepKind.poetry_project,
        ImageBuildStepKind.Pyproject: ImageStepKind.pyproject,
        ImageBuildStepKind.MicromambaEnvironment: ImageStepKind.micromamba_environment,
    }
    payload: dict[str, Any] = {"kind": kinds[step.kind].value}
    if step.command is not None:
        payload["command"] = step.command
    if step.args:
        payload["args"] = list(step.args)
    if step.groups:
        payload["groups"] = list(step.groups)
    return payload


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

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from shared.contracts import ContractModel

from images.building.credentials import (
    registry_auth_file_entry,
    unmarshal_registry_credentials,
)
from images.publication import (
    ImageBuildRegistryPushRequest,
    ImageBuildRegistryPushResult,
    ImageBuildRegistryPushStatus,
)


class RegistryPushProcessResult(ContractModel):
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


type RegistryPushCommandRunner = Callable[[Sequence[str]], RegistryPushProcessResult]
type RegistryInspectCommandRunner = Callable[
    [Sequence[str], int],
    RegistryPushProcessResult,
]


@dataclass(slots=True)
class SkopeoBaseImageDigestInspector:
    binary: str = "skopeo"
    timeout_seconds: int = 30
    tls_verify: bool = True
    runner: RegistryInspectCommandRunner | None = None

    def __call__(self, source_image: str, credentials: str) -> str:
        if not source_image.strip():
            raise ValueError("source image is required for registry inspection")
        with tempfile.TemporaryDirectory(prefix="lazycloud-registry-auth-") as temp_dir:
            command = [
                self.binary,
                "inspect",
                "--format",
                "{{.Digest}}",
                f"--tls-verify={str(self.tls_verify).lower()}",
            ]
            if credentials:
                authfile = _write_registry_auth_file(Path(temp_dir), credentials)
                command.extend(["--authfile", str(authfile)])
            command.append(f"docker://{source_image}")
            result = (self.runner or run_registry_inspect_command)(
                command,
                max(self.timeout_seconds, 1),
            )
        if not result.ok:
            detail = _combined_output(result) or result.reason
            suffix = f": {detail[:500]}" if detail else ""
            raise RuntimeError(
                f"registry inspection failed for {source_image} "
                f"with exit code {_exit_code_text(result.exit_code)}{suffix}"
            )
        digest = result.stdout.strip()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9+._-]*:[0-9A-Fa-f]{32,}", digest) is None:
            raise RuntimeError(f"registry inspection returned an invalid digest for {source_image}")
        return digest.lower()


@dataclass(slots=True)
class DockerRegistryPushClient:
    docker_binary: str = "docker"
    runner: RegistryPushCommandRunner | None = None

    def push(self, request: ImageBuildRegistryPushRequest) -> ImageBuildRegistryPushResult:
        if not request.source_ref:
            return ImageBuildRegistryPushResult(
                status=ImageBuildRegistryPushStatus.Skipped,
                reason="registry push has no source reference",
            )
        if not request.target_ref:
            return ImageBuildRegistryPushResult(
                status=ImageBuildRegistryPushStatus.Skipped,
                source_ref=request.source_ref,
                reason="registry push has no target reference",
            )

        metadata: dict[str, str] = {}
        if request.source_ref != request.target_ref:
            tag_command = [self.docker_binary, "tag", request.source_ref, request.target_ref]
            tag_result = self._runner(tag_command)
            metadata["registry_tag_command"] = shlex.join(tag_command)
            metadata["registry_tag_exit_code"] = _exit_code_text(tag_result.exit_code)
            if not tag_result.ok:
                return ImageBuildRegistryPushResult(
                    status=ImageBuildRegistryPushStatus.Error,
                    source_ref=request.source_ref,
                    target_ref=request.target_ref,
                    metadata=metadata,
                    reason=tag_result.reason or _combined_output(tag_result) or "image tag failed",
                )

        push_command = [self.docker_binary, "push", request.target_ref]
        push_result = self._runner(push_command)
        metadata["registry_push_command"] = shlex.join(push_command)
        metadata["registry_push_exit_code"] = _exit_code_text(push_result.exit_code)
        if not push_result.ok:
            return ImageBuildRegistryPushResult(
                status=ImageBuildRegistryPushStatus.Error,
                source_ref=request.source_ref,
                target_ref=request.target_ref,
                metadata=metadata,
                reason=push_result.reason or _combined_output(push_result) or "image push failed",
            )

        output = _combined_output(push_result)
        digest = docker_push_digest(output)
        return ImageBuildRegistryPushResult(
            status=ImageBuildRegistryPushStatus.Pushed,
            source_ref=request.source_ref,
            target_ref=request.target_ref,
            digest=digest,
            metadata=metadata,
            reason="image registry push complete",
        )

    def _runner(self, command: Sequence[str]) -> RegistryPushProcessResult:
        runner = self.runner or run_registry_push_command
        return runner(command)


def run_registry_push_command(command: Sequence[str]) -> RegistryPushProcessResult:
    try:
        completed = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return RegistryPushProcessResult(
            exit_code=127, stderr=str(exc), reason="registry tool not found"
        )
    return RegistryPushProcessResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_registry_inspect_command(
    command: Sequence[str],
    timeout_seconds: int,
) -> RegistryPushProcessResult:
    try:
        completed = subprocess.run(
            list(command),
            text=True,
            capture_output=True,
            timeout=max(timeout_seconds, 1),
            check=False,
        )
    except FileNotFoundError as exc:
        return RegistryPushProcessResult(
            exit_code=127,
            stderr=str(exc),
            reason="registry inspection tool not found",
        )
    except subprocess.TimeoutExpired as exc:
        return RegistryPushProcessResult(
            exit_code=None,
            stdout=_decoded_timeout_output(exc.stdout),
            stderr=_decoded_timeout_output(exc.stderr),
            reason="registry inspection timed out",
        )
    return RegistryPushProcessResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def docker_push_digest(output: str) -> str:
    match = re.search(r"digest:\s*(sha256:[0-9a-fA-F]{32,})", output)
    return match.group(1) if match is not None else ""


def _combined_output(result: RegistryPushProcessResult) -> str:
    return "\n".join(part for part in (result.stdout, result.stderr) if part).strip()


def _exit_code_text(exit_code: int | None) -> str:
    return "" if exit_code is None else str(exit_code)


def _write_registry_auth_file(directory: Path, credentials: str) -> Path:
    payload = unmarshal_registry_credentials(credentials)
    entry = registry_auth_file_entry(payload)
    if not payload.registry or not entry:
        raise ValueError("registry credentials did not produce an authfile entry")
    path = directory / "auth.json"
    path.write_text(
        json.dumps({"auths": {payload.registry: entry}}, separators=(",", ":")),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _decoded_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value

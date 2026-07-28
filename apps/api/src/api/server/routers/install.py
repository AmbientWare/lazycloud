from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agent.operations import (
    AgentInstallArch,
    AgentInstallOS,
    agent_binary_filename,
    build_agent_install_script,
)
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse, PlainTextResponse

from api.server.dependencies import current_services
from api.server.services import ApiServices

router = APIRouter()
_AGENT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@router.get("/install/agent", response_class=PlainTextResponse)
def install_agent_script(
    services: ApiServices = Depends(current_services),
) -> PlainTextResponse:
    artifact_settings = services.agent_binary_settings
    script = build_agent_install_script(
        binary_name=artifact_settings.binary_name,
        artifact_version=artifact_settings.artifact_version,
        sha256_by_arch=artifact_settings.sha256_by_arch,
    )
    return PlainTextResponse(
        script,
        media_type="text/x-shellscript; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/install/agent/{os_name}/{arch}", response_class=FileResponse)
def install_agent_binary(
    os_name: AgentInstallOS,
    arch: AgentInstallArch,
    services: ApiServices = Depends(current_services),
) -> FileResponse:
    artifact_settings = services.agent_binary_settings
    binary_dir = artifact_settings.binary_dir
    if binary_dir is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent binary artifacts are not configured",
        )
    path = _agent_binary_path(
        binary_dir,
        os_name,
        arch,
        binary_name=artifact_settings.binary_name,
    )
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent binary artifact not found",
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get(
    "/install/agent/{version}/{os_name}/{arch}",
    response_class=FileResponse,
)
def install_versioned_agent_binary(
    version: str,
    os_name: AgentInstallOS,
    arch: AgentInstallArch,
    services: ApiServices = Depends(current_services),
) -> FileResponse:
    artifact_settings = services.agent_binary_settings
    configured_version = artifact_settings.artifact_version
    if (
        not _AGENT_VERSION_PATTERN.fullmatch(version)
        or not configured_version
        or version != configured_version
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent binary version not found",
        )
    binary_dir = artifact_settings.binary_dir
    if binary_dir is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent binary artifacts are not configured",
        )
    path = _agent_binary_path(
        binary_dir / configured_version,
        os_name,
        arch,
        binary_name=artifact_settings.binary_name,
    )
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="agent binary artifact not found",
        )
    configured_sha256 = artifact_settings.sha256_by_arch.get(arch.value, "")
    if not configured_sha256 or _sha256(path) != configured_sha256:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent binary artifact failed integrity verification",
        )
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=path.name,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _agent_binary_path(
    binary_dir: Path,
    os_name: AgentInstallOS,
    arch: AgentInstallArch,
    *,
    binary_name: str,
) -> Path:
    return binary_dir / agent_binary_filename(os_name, arch, binary_name=binary_name)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

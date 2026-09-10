from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from agent.binary import AgentBinarySettings
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient


def _write_artifact(root: Path, version: str) -> Path:
    version_root = root / version
    version_root.mkdir(parents=True, exist_ok=True)
    path = version_root / "lazycloud-agent-linux-amd64"
    path.write_bytes(b"\x7fELF agent binary")
    return path


def test_versioned_agent_download_serves_only_the_configured_release(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    """A node bootstrapped by an older release must not be handed the current binary.

    The artifact directory is a mount, so it keeps whatever versions were staged
    into it; only the release this control plane resolved may be served from it.
    """

    configured_version = isolated_services.agent_binary_settings.binary_version
    artifact = _write_artifact(tmp_path, configured_version)
    data = artifact.read_bytes()
    _write_artifact(tmp_path, "0.0.1-previous")

    services = replace(
        isolated_services,
        agent_binary_settings=AgentBinarySettings(
            binary_dir=tmp_path,
            binary_version=configured_version,
            binary_sha256_by_arch={"amd64": sha256(data).hexdigest()},
        ),
    )
    with TestClient(create_app(services)) as client:
        previous = client.get("/install/agent/0.0.1-previous/linux/amd64")
        current = client.get(f"/install/agent/{configured_version}/linux/amd64")
        artifact.write_bytes(b"altered release bytes")
        corrupted = client.get(f"/install/agent/{configured_version}/linux/amd64")

    assert previous.status_code == 404
    assert previous.json()["detail"] == "agent binary version not found"
    assert current.status_code == 200
    assert current.content == data
    assert corrupted.status_code == 503

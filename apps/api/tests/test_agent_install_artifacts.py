from __future__ import annotations

from pathlib import Path

from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient


def _write_artifact(root: Path, version: str) -> None:
    version_root = root / version
    version_root.mkdir(parents=True, exist_ok=True)
    (version_root / "lazycloud-agent-linux-amd64").write_bytes(b"\x7fELF agent binary")


def test_versioned_agent_download_serves_only_the_configured_release(
    isolated_services: ApiServices,
    tmp_path: Path,
) -> None:
    """A node bootstrapped by an older release must not be handed the current binary.

    The artifact directory is a mount, so it keeps whatever versions were staged
    into it; only the release this control plane resolved may be served from it.
    """

    configured_version = isolated_services.agent_binary_settings.binary_version
    _write_artifact(tmp_path, configured_version)
    _write_artifact(tmp_path, "0.0.1-previous")

    with TestClient(create_app(isolated_services)) as client:
        previous = client.get("/install/agent/0.0.1-previous/linux/amd64")
        current = client.get(f"/install/agent/{configured_version}/linux/amd64")

    assert previous.status_code == 404
    assert previous.json()["detail"] == "agent binary version not found"
    # The configured version clears the version gate and is stopped only by the
    # digest the release published, which this placeholder artifact does not have.
    assert current.status_code == 503

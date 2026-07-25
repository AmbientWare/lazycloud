from __future__ import annotations

from pathlib import Path

import pytest
from api.fastapi_app import create_app
from api.server.services import ApiServices
from fastapi.testclient import TestClient


def test_web_static_serves_spa_without_masking_api_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_services: ApiServices,
) -> None:
    static_dir = tmp_path / "web"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>web-shell</body></html>")
    build_dir = static_dir / "_build"
    build_dir.mkdir()
    (build_dir / "app.js").write_text("console.log('ok')")
    monkeypatch.setenv("LAZYCLOUD_WEB_STATIC_DIR", str(static_dir))

    with TestClient(create_app(isolated_services)) as client:
        dashboard = client.get("/dashboard", headers={"accept": "text/html"})
        assert dashboard.status_code == 200
        assert "web-shell" in dashboard.text
        assert dashboard.headers["content-type"].startswith("text/html")

        asset = client.get("/_build/app.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text

        health = client.get("/health")
        assert health.status_code == 200
        assert health.headers["content-type"].startswith("application/json")

        missing_api = client.get("/api/does-not-exist", headers={"accept": "text/html"})
        assert missing_api.status_code == 404
        assert "web-shell" not in missing_api.text

        missing_api_post = client.post("/api/does-not-exist")
        assert missing_api_post.status_code == 404
        assert "web-shell" not in missing_api_post.text

        for backend_path in ("/auth/does-not-exist", "/gateway/does-not-exist"):
            missing_backend_get = client.get(backend_path, headers={"accept": "text/html"})
            assert missing_backend_get.status_code == 404
            assert "web-shell" not in missing_backend_get.text

            missing_backend_post = client.post(backend_path)
            assert missing_backend_post.status_code == 404
            assert "web-shell" not in missing_backend_post.text

        retired_bootstrap_get = client.get("/auth/bootstrap")
        assert retired_bootstrap_get.status_code == 404
        assert "web-shell" not in retired_bootstrap_get.text

        retired_bootstrap_post = client.post("/auth/bootstrap", json={"name": "root"})
        assert retired_bootstrap_post.status_code == 404
        assert "web-shell" not in retired_bootstrap_post.text

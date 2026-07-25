from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

WEB_STATIC_DIR_ENV = "LAZYCLOUD_WEB_STATIC_DIR"

_DEFAULT_STATIC_DIR = Path(__file__).parent / "web_static"
_BACKEND_PATH_NAMESPACES = frozenset({"api", "auth", "gateway"})


class _SpaStaticFiles(StaticFiles):
    """Static files with an index.html fallback for non-API SPA routes."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        if path.partition("/")[0] in _BACKEND_PATH_NAMESPACES:
            raise HTTPException(status_code=404)
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


def web_static_dir() -> Path:
    """Resolve the SPA build directory from the environment or package data."""
    configured = os.environ.get(WEB_STATIC_DIR_ENV, "")
    return Path(configured) if configured else _DEFAULT_STATIC_DIR


def mount_web_app(app: FastAPI) -> None:
    """Serve the built web app at the root when a SPA build is present."""
    static_dir = web_static_dir()
    if not (static_dir / "index.html").is_file():
        return
    app.mount("/", _SpaStaticFiles(directory=static_dir, html=True), name="web")

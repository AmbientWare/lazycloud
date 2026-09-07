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
_BACKEND_PATH_NAMESPACES = frozenset({"api", "auth", "gateway", "webhooks"})
"""Namespaces the SPA never answers for.

A path outside these falls through to the SPA document with a 200, which is right for
a client-side route and wrong for anything a machine calls: a payment provider
posting to a mistyped webhook path would read the dashboard's HTML as a
successful delivery and stop retrying.
"""
_BUILD_ASSET_NAMESPACE = "assets"
_SPA_DOCUMENT = "_shell.html"


class _SpaStaticFiles(StaticFiles):
    """Static files with the SPA document for non-API client routes.

    Caching is the load-bearing part. A build names each asset by its content, and a
    deploy replaces the whole set, so the previous build's names stop existing. The
    document that references them therefore must never be cached: a stale one asks
    for modules this deploy does not have, and the app fails to start for a client
    that did nothing wrong. The assets themselves are safe to keep forever, because
    a change to one changes its name.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        namespace = path.partition("/")[0]
        if namespace in _BACKEND_PATH_NAMESPACES:
            raise HTTPException(status_code=404)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            # A build asset is not a client-side route. Its name carries a content
            # hash, so a miss means that exact file is gone; answering with the
            # document hands the browser HTML where it asked for a module, and the
            # failure it then reports names neither the file nor the reason.
            if namespace == _BUILD_ASSET_NAMESPACE:
                raise
            response = await super().get_response(_SPA_DOCUMENT, scope)
        return _with_cache_policy(response, namespace=namespace)


_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_REVALIDATE_CACHE_CONTROL = "no-cache"


def _with_cache_policy(response: Response, *, namespace: str) -> Response:
    """Say how long an answer may be reused, since an intermediary otherwise guesses.

    Without this the responses carry only a validator, and a cache is free to decide
    a freshness lifetime heuristically. Doing that to the document is what turns one
    deploy into an app that will not start until the client clears its cache.
    """
    immutable = namespace == _BUILD_ASSET_NAMESPACE and response.status_code == 200
    response.headers["cache-control"] = (
        _IMMUTABLE_CACHE_CONTROL if immutable else _REVALIDATE_CACHE_CONTROL
    )
    return response


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

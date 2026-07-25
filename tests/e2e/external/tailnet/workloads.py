"""Endpoint fixture for a prepared Tailnet-backed compute pool.

The driving scenario exports the exact app and pool names before importing
this module. The production container re-imports the module only to resolve
the handler by ``module:function``, so missing variables fall back to inert
defaults instead of failing the import inside the paid container.
"""

from __future__ import annotations

import os

from lazycloud import App

app = App(os.environ.get("LAZYCLOUD_E2E_TAILNET_APP", "e2e_tailnet_route"))


@app.endpoint(
    name="tailnet-route",
    route="/tailnet/route",
    methods=["POST"],
    pool=os.environ.get("LAZYCLOUD_E2E_TAILNET_POOL", "tailnet"),
)
def tailnet_route(message: str) -> dict[str, str]:
    return {"message": message}


__all__ = ["tailnet_route"]

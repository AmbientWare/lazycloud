"""Acceptance: a bare ASGI callable served on a customer-owned domain.

Self-contained on purpose — the deployed source bundle carries this file, so an
import of a sibling package would fail inside the container rather than here.
"""

from __future__ import annotations

from typing import Any

from lazycloud import App, Image

site = App("site_test")

site_image = Image(python_version="3.12")

_PAGE = b"""<!doctype html>
<html><head><link rel="stylesheet" href="/style.css"></head>
<body><h1 id="served">upnext.run is served by LazyCloud</h1></body></html>
"""

_STYLE = b"#served { color: rebeccapurple }\n"


@site.asgi(name="web", domain="upnext.run", image=site_image, authorized=False)
async def web(scope: dict[str, Any], receive: Any, send: Any) -> None:
    del receive
    path = str(scope.get("path") or "/")
    body, content_type = (_STYLE, b"text/css") if path == "/style.css" else (_PAGE, b"text/html")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

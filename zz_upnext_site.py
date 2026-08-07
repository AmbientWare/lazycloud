"""Throwaway acceptance app: does a resource served on a customer-owned domain work?

Deliberately serves HTML that references an asset by absolute path. That is the case
the whole hostname design exists for — under a path prefix the browser would resolve
`/style.css` against the origin root and miss the resource entirely.
"""

from __future__ import annotations

from examples.asgi import ASGIMessage, ASGIReceive, ASGISend

from lazycloud import App, Image

site = App("upnext_site")

site_image = Image(python_version="3.12")

_PAGE = b"""<!doctype html>
<html><head><link rel="stylesheet" href="/style.css"></head>
<body><h1 id="served">upnext.run is served by LazyCloud</h1></body></html>
"""

_STYLE = b"#served { color: rebeccapurple }\n"


@site.asgi(name="site", domain="upnext.run", image=site_image)
async def app(scope: ASGIMessage, receive: ASGIReceive, send: ASGISend) -> None:
    path = scope.get("path", "/")
    body, content_type = (
        (_STYLE, b"text/css") if path == "/style.css" else (_PAGE, b"text/html")
    )
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

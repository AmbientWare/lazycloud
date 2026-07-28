"""Protocol-neutral response-header primitives.

These are HTTP header values rather than JSON payloads, so they sit beside
`shared.http` rather than inside it.
"""

from __future__ import annotations

from urllib.parse import quote

__all__ = [
    "INLINE_RENDERABLE_CONTENT_TYPES",
    "content_disposition",
]

# Types a browser renders without running script from the response's own
# origin. Anything outside this set is offered as a download instead, so a
# stored file cannot decide to execute in the dashboard's origin: the content
# type comes from whoever saved the file, not from us.
INLINE_RENDERABLE_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)

_FALLBACK_FILENAME = "download"


def content_disposition(filename: str, *, inline: bool = False) -> str:
    """Build a `Content-Disposition` value that survives any filename.

    Header values are Latin-1 and cannot carry control characters, so an
    ordinary name like `résumé.txt` has to go in the RFC 5987 `filename*`
    parameter with a plain-ASCII `filename` beside it for older readers.
    Interpolating the raw name instead fails the whole response.
    """
    disposition = "inline" if inline else "attachment"
    fallback = _ascii_fallback(filename)
    encoded = quote(filename, safe="")
    header = f'{disposition}; filename="{fallback}"'
    if encoded != fallback:
        header += f"; filename*=UTF-8''{encoded}"
    return header


def _ascii_fallback(filename: str) -> str:
    """The name reduced to printable ASCII, with quoting characters removed."""
    cleaned = "".join(
        character if 32 <= ord(character) < 127 and character not in '"\\' else "_"
        for character in filename
    )
    return cleaned.strip() or _FALLBACK_FILENAME

from __future__ import annotations

import os
import secrets
from pathlib import Path

from lazycloud import App, Image

APP_NAME = os.getenv("LAZYCLOUD_E2E_APP", f"cache_restart_{secrets.token_hex(6)}")
BUILD_MARKER = os.getenv("LAZYCLOUD_E2E_CACHE_MARKER", secrets.token_hex(12))
MARKER_PATH = Path("/opt/lazycloud/cache-e2e-marker")

app = App(APP_NAME)
image = Image(python_version="3.12").add_commands(
    [
        "install -d -m 0755 /opt/lazycloud && "
        f"printf '%s\\n' '{BUILD_MARKER}' > {MARKER_PATH} && chmod 0444 {MARKER_PATH}"
    ]
)


@app.function(name="cache-restart-probe", image=image, cpu=0.25, memory="128Mi")
def cache_restart_probe(expected: str) -> str:
    actual = MARKER_PATH.read_text(encoding="utf-8").strip()
    if actual != expected:
        raise RuntimeError("deployed image marker changed")
    return actual

from __future__ import annotations

import os
import secrets

from lazycloud import App, Image

APP_NAME = os.getenv("LAZYCLOUD_E2E_APP", f"artifact_{secrets.token_hex(6)}")
app = App(APP_NAME)


@app.function(name="artifact-owner", image=Image(python_version="3.12"), cpu=0.25, memory="128Mi")
def artifact_owner(marker: str) -> str:
    return marker

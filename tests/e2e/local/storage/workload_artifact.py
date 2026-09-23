from __future__ import annotations

import os
import secrets

from lazycloud import App, Image

APP_NAME = os.getenv("LAZYCLOUD_E2E_APP", f"artifact_{secrets.token_hex(6)}")
# A deployed container imports this module again, so it needs the names the
# caller generated or it would mint different ones.
DEPLOYMENT_ENV = {"LAZYCLOUD_E2E_APP": APP_NAME}

app = App(APP_NAME)


@app.function(
    name="artifact-owner",
    image=Image(python_version="3.12"),
    cpu=0.25,
    memory="128Mi",
    env=DEPLOYMENT_ENV,
)
def artifact_owner(marker: str) -> str:
    return marker

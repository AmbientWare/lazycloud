from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Literal

from lazycloud import App, Image, Volume

APP_NAME = os.getenv("LAZYCLOUD_E2E_APP", f"volume_mount_{secrets.token_hex(6)}")
VOLUME_NAME = os.getenv("LAZYCLOUD_E2E_VOLUME", f"volume-mount-{secrets.token_hex(6)}")
ROOT = Path("/mnt/e2e-volume")

# A deployed container imports this module again, so it needs the names the
# caller generated or it would mint different ones.
DEPLOYMENT_ENV = {"LAZYCLOUD_E2E_APP": APP_NAME, "LAZYCLOUD_E2E_VOLUME": VOLUME_NAME}

app = App(APP_NAME)
volume = Volume(VOLUME_NAME, str(ROOT))


@app.function(
    name="volume-probe",
    image=Image(python_version="3.12"),
    volumes=[volume],
    cpu=0.25,
    memory="128Mi",
    env=DEPLOYMENT_ENV,
)
def volume_probe(operation: Literal["read", "write"], relative_path: str, value: str = "") -> str:
    target = ROOT / relative_path
    if operation == "write":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    return target.read_text(encoding="utf-8")

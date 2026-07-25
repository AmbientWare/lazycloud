from __future__ import annotations

import os
import secrets

from lazycloud import App, Image

APP_NAME = f"private_registry_scope_{secrets.token_hex(6)}"
app = App(APP_NAME)
image = Image.from_registry(os.environ["LAZYCLOUD_E2E_PRIVATE_LOOKALIKE_IMAGE"]).add_commands(
    ["printf '%s' rejected > /lazycloud-private-registry-scope"]
)


@app.function(name="uncredentialed-lookalike", image=image, cpu=0.25, memory="256Mi")
def uncredentialed_lookalike() -> bool:
    return True

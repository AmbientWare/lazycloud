"""Two Functions on one image: one writes into its root, the other reads it back."""

from __future__ import annotations

import secrets

from lazycloud import App

# Outside any tmpfs. /tmp is not a separate mount, and a path at the root is
# inherited straight from the shared image directory.
ROOT_CANARY = "/lazycloud-e2e-canary"
TMP_CANARY = "/tmp/lazycloud-e2e-canary"

APP_NAME = f"e2e_rootfs_isolation_{secrets.token_hex(6)}"
app = App(APP_NAME)


@app.function(name="canary-writer", memory="128Mi", disk="2Gi", timeout_seconds=180)
def canary_writer(root_path: str, tmp_path: str, secret: str) -> dict[str, str]:
    """Write a secret into this container's root, then report which container ran."""
    import socket
    from pathlib import Path

    Path(root_path).write_text(secret, encoding="utf-8")
    Path(tmp_path).write_text(secret, encoding="utf-8")
    return {"container": socket.gethostname()}


@app.function(name="canary-reader", memory="128Mi", disk="2Gi", timeout_seconds=180)
def canary_reader(root_path: str, tmp_path: str) -> dict[str, str]:
    """Report whatever an earlier container on this image may have left behind."""
    import socket
    from pathlib import Path

    def read(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return ""

    return {
        "container": socket.gethostname(),
        "root_canary": read(root_path),
        "tmp_canary": read(tmp_path),
    }

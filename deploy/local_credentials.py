"""Fill missing local infrastructure credentials without replacing existing values."""

from __future__ import annotations

import os
import re
import secrets
import sys
import tempfile
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1]).resolve()
    content = path.read_text()
    original = content
    values = {
        "LAZYCLOUD_TOKEN": f"rt_{secrets.token_urlsafe(32)}",
        "LAZYCLOUD_TUNNEL_GATEWAY_BOOTSTRAP_SECRET": secrets.token_urlsafe(48),
        "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID": f"GK{secrets.token_hex(12)}",
        "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY": secrets.token_hex(32),
        "LAZYCLOUD_GARAGE_ADMIN_TOKEN": secrets.token_urlsafe(48),
        "LAZYCLOUD_GARAGE_RPC_SECRET": secrets.token_hex(32),
    }
    for name, value in values.items():
        pattern = rf"^{name}=(.*)$"
        match = re.search(pattern, content, flags=re.MULTILINE)
        if match is None:
            content += f"\n{name}={value}\n"
        elif not match[1].strip().strip("\"'"):
            content = re.sub(pattern, f"{name}={value}", content, flags=re.MULTILINE)
    if content == original:
        path.chmod(0o600)
        return
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=".env-")
    try:
        with os.fdopen(handle, "w") as stream:
            stream.write(content)
        os.replace(staged, path)
    finally:
        Path(staged).unlink(missing_ok=True)


if __name__ == "__main__":
    main()

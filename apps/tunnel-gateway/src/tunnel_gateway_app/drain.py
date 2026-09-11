from __future__ import annotations

import logging
import time

from observability.process_logs import configure_process_logging

from tunnel_gateway_app.main import TunnelGatewaySettings

LOGGER = logging.getLogger(__name__)


def main() -> None:
    configure_process_logging()
    settings = TunnelGatewaySettings()
    settings.drain_file.touch(mode=0o600, exist_ok=True)
    started = time.monotonic()
    while time.monotonic() - started < 130:
        ready = settings.ready_file.exists()
        LOGGER.info(
            "WireGuard termination poll index=%s serving=%s elapsed=%.1fs",
            settings.gateway_index,
            ready,
            time.monotonic() - started,
        )
        if not ready:
            return
        time.sleep(2)
    raise RuntimeError("WireGuard gateway did not complete its drain within 130 seconds")


if __name__ == "__main__":
    main()

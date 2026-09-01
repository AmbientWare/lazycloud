from __future__ import annotations

import logging
import signal
import subprocess
import threading
from datetime import timedelta
from pathlib import Path

from networking.wireguard import (
    WIREGUARD_AGENT_NETWORK,
    WIREGUARD_GATEWAY_ADDRESS,
    WireGuardClientRuntime,
    WireGuardPeerConfiguration,
    validate_wireguard_public_key,
    wireguard_platform_address,
    wireguard_platform_index,
)
from observability.process_logs import configure_process_logging
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)


class PlatformClientSettings(BaseSettings):
    private_key_file: Path = Path()
    server_public_key_file: Path = Path()
    gateway_endpoint: str = ""
    platform_index: int | None = Field(default=None, ge=0, le=31)
    pod_name: str = ""
    ready_file: Path = Path("/tmp/lazycloud-wireguard-platform.ready")
    poll_interval_seconds: float = Field(default=2.0, ge=0.25, le=30)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_WIREGUARD_",
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_identity(self) -> PlatformClientSettings:
        if str(self.private_key_file) in {"", "."} or str(self.server_public_key_file) in {
            "",
            ".",
        }:
            raise ValueError("WireGuard key paths are required")
        self.gateway_endpoint = self.gateway_endpoint.strip()
        if not self.gateway_endpoint:
            raise ValueError("WireGuard gateway endpoint is required")
        if self.platform_index is None and not self.pod_name.strip():
            raise ValueError("WireGuard platform index or pod name is required")
        return self


def main() -> None:
    configure_process_logging()
    settings = PlatformClientSettings()
    index = wireguard_platform_index(settings.platform_index, pod_name=settings.pod_name)
    runtime = WireGuardClientRuntime(settings.private_key_file.parent)
    server_public_key = validate_wireguard_public_key(
        settings.server_public_key_file.read_text(encoding="utf-8")
    )
    runtime.configure(
        WireGuardPeerConfiguration(
            peer_id=f"platform-{index}",
            address=f"{wireguard_platform_address(index)}/32",
            server_public_key=server_public_key,
            endpoint=settings.gateway_endpoint,
            allowed_ips=(str(WIREGUARD_AGENT_NETWORK),),
            generation=1,
        )
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stop.is_set():
            handshake = runtime.latest_handshake_at(server_public_key)
            healthy = handshake is not None and utc_now() - handshake < timedelta(minutes=2)
            if healthy:
                settings.ready_file.touch(mode=0o600, exist_ok=True)
            else:
                settings.ready_file.unlink(missing_ok=True)
                subprocess.run(
                    ["ping", "-c", "1", "-W", "1", str(WIREGUARD_GATEWAY_ADDRESS)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            LOGGER.info(
                "WireGuard platform poll index=%s handshake=%s healthy=%s",
                index,
                handshake.isoformat() if handshake else "none",
                healthy,
            )
            stop.wait(settings.poll_interval_seconds)
    finally:
        settings.ready_file.unlink(missing_ok=True)
        runtime.close()


if __name__ == "__main__":
    main()

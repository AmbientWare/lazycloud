from __future__ import annotations

import logging
import signal
import threading
from pathlib import Path
from time import monotonic

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient
from database.client import DatabaseClient
from database.repositories.compute import WireGuardGatewayRepository, WireGuardPeerRepository
from database.settings import DatabaseApplicationName, DatabaseSettings
from networking.wireguard import (
    WIREGUARD_AGENT_NETWORK,
    wireguard_platform_address,
    wireguard_platform_index,
)
from networking.wireguard_client import WireGuardClientRuntime
from networking.wireguard_state import (
    WIREGUARD_PRESENCE_TTL_SECONDS,
    WireGuardGatewayPresenceRepository,
)
from observability.process_logs import configure_process_logging
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.http.private_network import (
    WireGuardGatewayConfiguration,
    WireGuardPeerConfiguration,
    WireGuardRouteConfiguration,
)
from sqlalchemy.exc import OperationalError

LOGGER = logging.getLogger(__name__)


class PlatformClientSettings(BaseSettings):
    private_key_file: Path = Path()
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
        if str(self.private_key_file) in {"", "."}:
            raise ValueError("WireGuard key paths are required")
        if self.platform_index is None and not self.pod_name.strip():
            raise ValueError("WireGuard platform index or pod name is required")

        return self


def _configuration(
    database: DatabaseClient,
    presence: WireGuardGatewayPresenceRepository,
    index: int,
) -> WireGuardPeerConfiguration | None:
    with database.session() as session:
        gateways = WireGuardGatewayRepository(session).list_all()
        peers = WireGuardPeerRepository(session).active()
    if not gateways:
        return None
    try:
        observed = presence.list_current(tuple(gateway.index for gateway in gateways))
    except REDIS_UNAVAILABLE_ERRORS:
        LOGGER.warning("WireGuard gateway presence unavailable; new destination routes withdrawn")
        observed = ()
    paths: dict[tuple[str, int], list[int]] = {}
    for gateway in observed:
        if gateway.draining:
            continue
        for peer in gateway.peers:
            paths.setdefault((peer.peer_id, peer.generation), []).append(gateway.index)
    return WireGuardPeerConfiguration(
        peer_id=f"platform-{index}",
        address=f"{wireguard_platform_address(index)}/32",
        allowed_ips=(str(WIREGUARD_AGENT_NETWORK),),
        generation=1,
        gateways=tuple(
            WireGuardGatewayConfiguration(
                index=gateway.index,
                public_key=gateway.public_key,
                endpoint=gateway.endpoint,
            )
            for gateway in gateways
        ),
        routes=tuple(
            WireGuardRouteConfiguration(
                network=peer.address,
                gateway_indices=tuple(paths[(peer.id, peer.generation)]),
            )
            for peer in peers
            if (peer.id, peer.generation) in paths
        ),
    )


def main() -> None:
    configure_process_logging()
    settings = PlatformClientSettings()
    index = wireguard_platform_index(settings.platform_index, pod_name=settings.pod_name)
    runtime = WireGuardClientRuntime(settings.private_key_file.parent)
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.TunnelGateway)
    )
    redis = RedisClient.from_settings()
    presence = WireGuardGatewayPresenceRepository(redis)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    configuration: WireGuardPeerConfiguration | None = None
    try:
        settings.ready_file.unlink(missing_ok=True)
        while not stop.is_set():
            try:
                observed = _configuration(database, presence, index)
            except OperationalError:
                LOGGER.warning("WireGuard registry unavailable; new destination routes withdrawn")
                observed = None
            if observed is not None:
                configuration = observed
            elif configuration is not None:
                configuration = configuration.model_copy(update={"routes": ()})
            else:
                LOGGER.info("WireGuard platform poll index=%s gateways=0 healthy=false", index)
                stop.wait(settings.poll_interval_seconds)
                continue
            runtime.configure(
                configuration,
                route_deadline=monotonic() + WIREGUARD_PRESENCE_TTL_SECONDS,
            )
            healthy = runtime.reconcile_connection(configuration)
            if healthy:
                settings.ready_file.touch(mode=0o600, exist_ok=True)
            else:
                settings.ready_file.unlink(missing_ok=True)
            LOGGER.info(
                "WireGuard platform poll index=%s paths=%s destinations=%s healthy=%s",
                index,
                runtime.path_health(),
                len(configuration.routes),
                healthy,
            )
            stop.wait(settings.poll_interval_seconds)
    finally:
        settings.ready_file.unlink(missing_ok=True)
        runtime.close()
        redis.close()
        database.dispose()


if __name__ == "__main__":
    main()

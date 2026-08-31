from __future__ import annotations

import logging
import signal
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from uuid import uuid4

from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient, RedisSettings
from coordination.token_lock import (
    release_token_lock,
    renew_token_lock,
    try_acquire_token_lock,
)
from database.client import DatabaseClient
from database.repositories.compute import (
    PRIMARY_WIREGUARD_GATEWAY_ID,
    ComputeMachineEnrollmentRepository,
    WireGuardGatewayRepository,
    WireGuardPeerRepository,
)
from database.settings import DatabaseApplicationName, DatabaseSettings
from networking.wireguard import WireGuardError, wireguard_platform_address
from networking.wireguard_gateway import WireGuardGatewayPeer, WireGuardGatewayRuntime
from observability.process_logs import configure_process_logging
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.compute_enrollment import PrivateNetworkEnrollmentPhase, WireGuardGateway
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)


class TunnelGatewaySettings(BaseSettings):
    private_key_file: Path = Path()
    public_endpoint: str = ""
    platform_key_directory: Path = Path()
    platform_peer_count: int = Field(default=2, ge=1, le=32)
    ready_file: Path = Path("/tmp/lazycloud-tunnel-gateway.ready")
    health_port: int = Field(default=8080, ge=1, le=65535)
    lease_ttl_seconds: int = Field(default=10, ge=4, le=120)
    reconcile_interval_seconds: float = Field(default=2.0, ge=0.25, le=30)

    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_WIREGUARD_",
        extra="ignore",
    )

    @field_validator("public_endpoint")
    @classmethod
    def validate_public_endpoint(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("WireGuard public endpoint is required")
        return normalized

    @field_validator("private_key_file", "platform_key_directory")
    @classmethod
    def validate_path(cls, value: Path) -> Path:
        if str(value) in {"", "."}:
            raise ValueError("WireGuard key paths are required")
        return value


@dataclass(frozen=True, slots=True)
class StaticWireGuardPeer:
    public_key: str
    address: str


class _TcpHealthListener:
    def __init__(self, port: int) -> None:
        self._port = port
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._listener is not None:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("0.0.0.0", self._port))
                listener.listen(8)
                listener.settimeout(0.25)
            except OSError:
                listener.close()
                raise
            thread = threading.Thread(
                target=self._serve,
                args=(listener,),
                name="wireguard-health",
                daemon=True,
            )
            self._listener = listener
            self._thread = thread
            thread.start()

    def close(self) -> None:
        with self._lock:
            listener = self._listener
            thread = self._thread
            self._listener = None
            self._thread = None
        if listener is not None:
            listener.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    @staticmethod
    def _serve(listener: socket.socket) -> None:
        while True:
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            connection.close()


class _GatewayLease:
    def __init__(
        self,
        *,
        redis: RedisClient,
        key: str,
        token: str,
        ttl_seconds: int,
        acquired_at: float,
        ready_file: Path,
        health_port: int,
        runtime: WireGuardGatewayRuntime,
    ) -> None:
        self._redis = redis
        self._key = key
        self._token = token
        self._ttl_seconds = ttl_seconds
        self._ready_file = ready_file
        self._health = _TcpHealthListener(health_port)
        self._runtime = runtime
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._confirmed_until = acquired_at + ttl_seconds
        self._renewal_thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None

    @property
    def fresh(self) -> bool:
        with self._state_lock:
            return not self._lost.is_set() and monotonic() < self._confirmed_until

    def start(self) -> None:
        if not self.fresh:
            self._lose("WireGuard gateway lease expired during acquisition")
            return
        self._renewal_thread = threading.Thread(
            target=self._renew,
            name="wireguard-lease-renewal",
            daemon=True,
        )
        self._watchdog_thread = threading.Thread(
            target=self._watch,
            name="wireguard-lease-watchdog",
            daemon=True,
        )
        self._renewal_thread.start()
        self._watchdog_thread.start()

    def mark_ready(self) -> bool:
        with self._state_lock:
            if not self._lost.is_set() and monotonic() < self._confirmed_until:
                self._health.start()
                self._ready_file.touch(mode=0o600, exist_ok=True)
                return True
        self._lose("WireGuard gateway lease expired before readiness")
        return False

    def close(self) -> None:
        self._stop.set()
        self._lose(None)
        for thread in (self._renewal_thread, self._watchdog_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=1)

    def _renew(self) -> None:
        interval = self._ttl_seconds / 3
        while not self._stop.wait(interval):
            attempted_at = monotonic()
            try:
                renewed = renew_token_lock(
                    self._redis,
                    self._key,
                    self._token,
                    ttl_seconds=self._ttl_seconds,
                )
            except REDIS_UNAVAILABLE_ERRORS:
                LOGGER.exception("WireGuard gateway lease renewal failed")
                self._lose("WireGuard gateway lease freshness could not be confirmed")
                return
            if not renewed:
                self._lose("WireGuard gateway lease lost")
                return
            confirmed_until = attempted_at + self._ttl_seconds
            with self._state_lock:
                if self._lost.is_set():
                    return
                if monotonic() >= confirmed_until:
                    expired = True
                else:
                    self._confirmed_until = confirmed_until
                    expired = False
            if expired:
                self._lose("WireGuard gateway lease renewal completed after expiry")
                return

    def _watch(self) -> None:
        while not self._stop.is_set():
            with self._state_lock:
                if self._lost.is_set():
                    return
                remaining = self._confirmed_until - monotonic()
            if remaining <= 0:
                self._lose("WireGuard gateway lease freshness expired")
                return
            self._stop.wait(remaining)

    def _lose(self, message: str | None) -> None:
        with self._state_lock:
            if self._lost.is_set():
                return
            self._lost.set()
        if message is not None:
            LOGGER.warning(message)
        try:
            self._ready_file.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not remove WireGuard gateway readiness file")
        self._health.close()
        self._runtime.close()


@dataclass(slots=True)
class TunnelGatewayProcess:
    database: DatabaseClient
    redis: RedisClient
    runtime: WireGuardGatewayRuntime
    platform_peers: tuple[WireGuardGatewayPeer, ...]
    settings: TunnelGatewaySettings
    stop: threading.Event

    def run(self) -> None:
        token = uuid4().hex
        lease_key = self.redis.key("wireguard", "gateway", "active")
        lease: _GatewayLease | None = None
        self.settings.ready_file.unlink(missing_ok=True)
        try:
            while not self.stop.is_set():
                if lease is None:
                    attempted_at = monotonic()
                    acquired = try_acquire_token_lock(
                        self.redis,
                        lease_key,
                        token,
                        ttl_seconds=self.settings.lease_ttl_seconds,
                    )
                    if acquired:
                        lease = _GatewayLease(
                            redis=self.redis,
                            key=lease_key,
                            token=token,
                            ttl_seconds=self.settings.lease_ttl_seconds,
                            acquired_at=attempted_at,
                            ready_file=self.settings.ready_file,
                            health_port=self.settings.health_port,
                            runtime=self.runtime,
                        )
                        lease.start()
                        self.runtime.start()
                        if not lease.fresh:
                            self._close_lease(lease, lease_key, token, release=False)
                            lease = None
                            continue
                        LOGGER.info("WireGuard gateway lease acquired")
                    else:
                        LOGGER.info("WireGuard gateway poll active=false")
                        self.stop.wait(self.settings.reconcile_interval_seconds)
                        continue
                try:
                    peers, observed = self._reconcile()
                except WireGuardError:
                    if lease.fresh:
                        raise
                    self._close_lease(lease, lease_key, token, release=False)
                    lease = None
                    continue
                if not lease.fresh:
                    self._close_lease(lease, lease_key, token, release=False)
                    lease = None
                    continue
                self._publish_gateway(self.settings.public_endpoint)
                if not lease.mark_ready():
                    self._close_lease(lease, lease_key, token, release=False)
                    lease = None
                    continue
                LOGGER.info(
                    "WireGuard gateway poll active=true peers=%s handshakes=%s",
                    peers,
                    observed,
                )
                self.stop.wait(self.settings.reconcile_interval_seconds)
        finally:
            if lease is None:
                self.settings.ready_file.unlink(missing_ok=True)
            else:
                self._close_lease(lease, lease_key, token, release=lease.fresh)

    def _close_lease(
        self,
        lease: _GatewayLease,
        lease_key: str,
        token: str,
        *,
        release: bool,
    ) -> None:
        lease.close()
        self.runtime.close()
        if release:
            release_token_lock(self.redis, lease_key, token)

    def _reconcile(self) -> tuple[int, int]:
        with self.database.session() as session:
            peers = WireGuardPeerRepository(session).active()
        self.runtime.reconcile((*self.platform_peers, *peers))
        handshakes = self.runtime.handshakes()
        changed = 0
        with self.database.session() as session:
            peer_repository = WireGuardPeerRepository(session)
            enrollments = ComputeMachineEnrollmentRepository(session)
            for peer in peers:
                observed = handshakes.get(peer.public_key)
                if observed is None or peer.last_handshake_at is not None:
                    continue
                current = peer_repository.by_enrollment(peer.enrollment_id, for_update=True)
                if (
                    current is None
                    or current.public_key != peer.public_key
                    or current.last_handshake_at is not None
                ):
                    continue
                now = utc_now()
                peer_repository.save(
                    current.model_copy(
                        update={
                            "last_handshake_at": observed,
                            "updated_at": now,
                        }
                    )
                )
                enrollment = enrollments.by_machine(
                    peer.workspace_id,
                    peer.machine_id,
                    for_update=True,
                )
                if enrollment is not None and enrollment.network_peer_id == peer.id:
                    enrollments.save(
                        enrollment.model_copy(
                            update={
                                "network_phase": PrivateNetworkEnrollmentPhase.Connected,
                                "network_verified_at": observed,
                                "updated_at": now,
                            }
                        )
                    )
                changed += 1
        return len(peers), changed

    def _publish_gateway(self, endpoint: str) -> None:
        public_key = self.runtime.public_key()
        with self.database.session() as session:
            repository = WireGuardGatewayRepository(session)
            current = repository.current()
            if current is not None and (
                current.public_key == public_key and current.endpoint == endpoint
            ):
                return
            repository.save(
                WireGuardGateway(
                    id=PRIMARY_WIREGUARD_GATEWAY_ID,
                    public_key=public_key,
                    endpoint=endpoint,
                    updated_at=utc_now(),
                )
            )


def _platform_peers(settings: TunnelGatewaySettings) -> tuple[WireGuardGatewayPeer, ...]:
    peers: list[WireGuardGatewayPeer] = []
    for index in range(settings.platform_peer_count):
        path = settings.platform_key_directory / f"platform-{index}" / "public-key"
        public_key = path.read_text(encoding="utf-8").strip()
        peers.append(
            StaticWireGuardPeer(
                public_key=public_key,
                address=f"{wireguard_platform_address(index)}/32",
            )
        )
    return tuple(peers)


def main() -> None:
    configure_process_logging()
    settings = TunnelGatewaySettings()
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.TunnelGateway)
    )
    redis = RedisClient.from_settings(RedisSettings())
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        TunnelGatewayProcess(
            database=database,
            redis=redis,
            runtime=WireGuardGatewayRuntime(settings.private_key_file),
            platform_peers=_platform_peers(settings),
            settings=settings,
            stop=stop,
        ).run()
    finally:
        redis.close()
        database.dispose()


if __name__ == "__main__":
    main()

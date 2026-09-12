from __future__ import annotations

import logging
import signal
import socket
import threading
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface
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
    WireGuardGatewayRepository,
    WireGuardPeerRepository,
    wireguard_gateway_id,
)
from database.settings import DatabaseApplicationName, DatabaseSettings
from networking.wireguard import (
    WIREGUARD_GATEWAY_ADDRESS,
    WIREGUARD_GATEWAY_HEALTH_PORT,
    WIREGUARD_OVERLAY,
    WireGuardError,
    wireguard_platform_address,
)
from networking.wireguard_gateway import (
    WireGuardGatewayPeer,
    WireGuardGatewayRuntime,
    WireGuardRuntimeService,
)
from networking.wireguard_state import (
    WIREGUARD_PRESENCE_TTL_SECONDS,
    WireGuardGatewayPresence,
    WireGuardGatewayPresenceRepository,
    WireGuardPeerPresence,
)
from observability.process_logs import configure_process_logging
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.app_identity import ENV_PREFIX
from shared.compute_enrollment import WireGuardGateway
from shared.timestamps import utc_now

LOGGER = logging.getLogger(__name__)
RUNTIME_SERVICE_DNS_REFRESH_SECONDS = 10.0


class TunnelGatewaySettings(BaseSettings):
    gateway_index: int = Field(default=-1, ge=0, le=31)
    runtime_service_host: str = Field(default="", min_length=1, max_length=253)
    runtime_service_port: int = Field(default=0, ge=1, le=65535)
    private_key_file: Path = Path()
    public_endpoint: str = ""
    platform_key_directory: Path = Path()
    platform_peer_count: int = Field(default=2, ge=1, le=32)
    ready_file: Path = Path("/tmp/lazycloud-tunnel-gateway.ready")
    drain_file: Path = Path("/tmp/lazycloud-tunnel-gateway.drain")
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

    @field_validator("runtime_service_host")
    @classmethod
    def validate_runtime_service_host(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "/:@\\\n\r\t "):
            raise ValueError("runtime Service host must be an IPv4 address or DNS hostname")
        return value


@dataclass(slots=True)
class RuntimeServiceResolver:
    host: str
    port: int
    _target: WireGuardRuntimeService | None = None
    _refresh_after: float = 0.0

    def resolve(self) -> WireGuardRuntimeService:
        now = monotonic()
        if self._target is not None and now < self._refresh_after:
            return self._target
        self._refresh_after = now + RUNTIME_SERVICE_DNS_REFRESH_SECONDS
        try:
            addresses = {
                IPv4Address(address[4][0])
                for address in socket.getaddrinfo(
                    self.host, self.port, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            }
        except socket.gaierror as exc:
            if self._target is None:
                raise WireGuardError(f"could not resolve runtime Service host {self.host}") from exc
            LOGGER.warning(
                "Runtime Service DNS refresh failed host=%s address=%s reason=%s",
                self.host,
                self._target.address,
                exc,
            )
            return self._target
        if len(addresses) != 1:
            raise WireGuardError("runtime Service host must resolve to exactly one IPv4 address")
        target = WireGuardRuntimeService(address=addresses.pop(), port=self.port)
        if target != self._target:
            LOGGER.info(
                "Runtime Service resolved host=%s address=%s port=%s",
                self.host,
                target.address,
                target.port,
            )
        self._target = target
        return target


@dataclass(frozen=True, slots=True)
class StaticWireGuardPeer:
    public_key: str
    address: str
    generation: int = 1


@dataclass(slots=True)
class _PeerProbe:
    public_key: str
    generation: int
    observed_at: float | None = None


class _TcpHealthListener:
    def __init__(self, port: int) -> None:
        self._port = port
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._draining = threading.Event()
        self._peers: dict[IPv4Address, _PeerProbe] = {}

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
            self._peers.clear()
        if listener is not None:
            listener.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def drain(self) -> None:
        self._draining.set()

    def connected_peers(
        self,
        peers: Sequence[WireGuardGatewayPeer],
        handshake_keys: Collection[str],
    ) -> set[tuple[str, int]]:
        with self._lock:
            current: dict[IPv4Address, _PeerProbe] = {}
            for peer in peers:
                address = IPv4Interface(peer.address)
                if address.network.prefixlen != 32 or address.ip not in WIREGUARD_OVERLAY:
                    raise WireGuardError("WireGuard health peer requires an overlay /32 address")
                previous = self._peers.get(address.ip)
                if (
                    previous is not None
                    and previous.public_key == peer.public_key
                    and previous.generation == peer.generation
                ):
                    current[address.ip] = previous
                else:
                    current[address.ip] = _PeerProbe(peer.public_key, peer.generation)
            self._peers = current
            cutoff = monotonic() - WIREGUARD_PRESENCE_TTL_SECONDS
            return {
                (peer.public_key, peer.generation)
                for peer in current.values()
                if peer.observed_at is not None
                and peer.observed_at >= cutoff
                and peer.public_key in handshake_keys
            }

    def _serve(self, listener: socket.socket) -> None:
        while True:
            try:
                connection, remote = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                source = IPv4Address(remote[0])
                encrypted = IPv4Address(connection.getsockname()[0]) == WIREGUARD_GATEWAY_ADDRESS
                with self._lock:
                    peer = self._peers.get(source) if encrypted else None
                try:
                    connection.settimeout(0.25)
                    connection.sendall(b"draining\n" if self._draining.is_set() else b"ready\n")
                except OSError:
                    pass
                else:
                    with self._lock:
                        if peer is not None and self._peers.get(source) is peer:
                            peer.observed_at = monotonic()


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

    @property
    def cancelled(self) -> threading.Event:
        return self._lost

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

    def mark_ready(self, *, platform_connected: bool) -> bool:
        with self._state_lock:
            if not self._lost.is_set() and monotonic() < self._confirmed_until:
                self._health.start()
                if platform_connected:
                    self._ready_file.touch(mode=0o600, exist_ok=True)
                else:
                    self._ready_file.unlink(missing_ok=True)
                return True
        self._lose("WireGuard gateway lease expired before readiness")
        return False

    def drain(self) -> None:
        self._health.drain()

    def connected_peers(
        self,
        peers: Sequence[WireGuardGatewayPeer],
        handshake_keys: Collection[str],
    ) -> set[tuple[str, int]]:
        return self._health.connected_peers(peers, handshake_keys)

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
            except REDIS_UNAVAILABLE_ERRORS as exc:
                LOGGER.warning("WireGuard gateway lease renewal failed: %s", type(exc).__name__)
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
    runtime_service_resolver: RuntimeServiceResolver
    platform_peers: tuple[WireGuardGatewayPeer, ...]
    settings: TunnelGatewaySettings
    stop: threading.Event

    def run(self) -> None:
        token = uuid4().hex
        presence = WireGuardGatewayPresenceRepository(self.redis)
        lease_key = presence.lease_key(self.settings.gateway_index)
        lease: _GatewayLease | None = None
        drain_started: float | None = None
        self.settings.ready_file.unlink(missing_ok=True)
        self.settings.drain_file.unlink(missing_ok=True)
        try:
            while not self.stop.is_set():
                if self.settings.drain_file.exists() and lease is None:
                    break
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
                            health_port=WIREGUARD_GATEWAY_HEALTH_PORT,
                            runtime=self.runtime,
                        )
                        lease.start()
                        self.runtime.start(cancelled=lease.cancelled)
                        if not lease.fresh:
                            self._close_lease(lease, lease_key, token, release=False)
                            lease = None
                            continue
                        LOGGER.info(
                            "WireGuard gateway lease acquired index=%s", self.settings.gateway_index
                        )
                    else:
                        LOGGER.info("WireGuard gateway poll active=false")
                        self.stop.wait(self.settings.reconcile_interval_seconds)
                        continue
                try:
                    peers, observed, paths, platform_connected = self._reconcile(lease)
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
                draining = self.settings.drain_file.exists()
                if not presence.publish(
                    WireGuardGatewayPresence(
                        index=self.settings.gateway_index,
                        owner_token=token,
                        draining=draining,
                        peers=paths,
                    )
                ):
                    self._close_lease(lease, lease_key, token, release=False)
                    lease = None
                    continue
                if draining:
                    lease.drain()
                if not lease.mark_ready(platform_connected=platform_connected):
                    self._close_lease(lease, lease_key, token, release=False)
                    lease = None
                    continue
                if draining:
                    if drain_started is None:
                        drain_started = monotonic()
                    elapsed = monotonic() - drain_started
                    connections = self.runtime.active_connections()
                    LOGGER.info(
                        "WireGuard gateway drain index=%s connections=%s elapsed=%.1fs",
                        self.settings.gateway_index,
                        connections,
                        elapsed,
                    )
                    if elapsed >= 120 or (elapsed >= 10 and connections == 0):
                        if connections:
                            LOGGER.warning(
                                "WireGuard drain deadline reached with %s connections", connections
                            )
                        break
                LOGGER.info(
                    "WireGuard gateway poll index=%s active=true peers=%s handshakes=%s "
                    "platform_connected=%s",
                    self.settings.gateway_index,
                    peers,
                    observed,
                    platform_connected,
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
        try:
            WireGuardGatewayPresenceRepository(self.redis).remove(
                self.settings.gateway_index, token
            )
            if release:
                release_token_lock(self.redis, lease_key, token)
        except REDIS_UNAVAILABLE_ERRORS:
            LOGGER.warning(
                "WireGuard gateway cleanup could not reach Redis; ownership will expire index=%s",
                self.settings.gateway_index,
            )

    def _reconcile(
        self,
        lease: _GatewayLease,
    ) -> tuple[int, int, tuple[WireGuardPeerPresence, ...], bool]:
        self.runtime.reconcile_runtime_service(self.runtime_service_resolver.resolve())
        with self.database.session() as session:
            peers = WireGuardPeerRepository(session).active()
        configured = (*self.platform_peers, *peers)
        self.runtime.reconcile(configured)
        handshakes = self.runtime.handshakes()
        connected = lease.connected_peers(configured, handshakes.keys())
        changed = 0
        with self.database.session() as session:
            peer_repository = WireGuardPeerRepository(session)
            for peer in peers:
                observed = handshakes.get(peer.public_key)
                if observed is None or peer.last_handshake_at is not None:
                    continue
                current = peer_repository.by_enrollment(peer.enrollment_id, for_update=True)
                if (
                    current is None
                    or current.public_key != peer.public_key
                    or current.generation != peer.generation
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
                changed += 1
        paths = tuple(
            WireGuardPeerPresence(peer_id=peer.id, generation=peer.generation)
            for peer in peers
            if (peer.public_key, peer.generation) in connected
        )
        platform_connected = all(
            (peer.public_key, peer.generation) in connected for peer in self.platform_peers
        )
        return len(peers), changed, paths, platform_connected

    def _publish_gateway(self, endpoint: str) -> None:
        public_key = self.runtime.public_key()
        with self.database.session() as session:
            repository = WireGuardGatewayRepository(session)
            current = repository.get_by_index(self.settings.gateway_index)
            if current is not None and (
                current.public_key == public_key and current.endpoint == endpoint
            ):
                return
            repository.save(
                WireGuardGateway(
                    id=wireguard_gateway_id(self.settings.gateway_index),
                    index=self.settings.gateway_index,
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
    runtime_service_resolver = RuntimeServiceResolver(
        settings.runtime_service_host, settings.runtime_service_port
    )
    runtime_service = runtime_service_resolver.resolve()
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.TunnelGateway)
    )
    redis = RedisClient.from_settings(RedisSettings())
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        settings.drain_file.touch(mode=0o600, exist_ok=True)

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        TunnelGatewayProcess(
            database=database,
            redis=redis,
            runtime=WireGuardGatewayRuntime(settings.private_key_file, runtime_service),
            runtime_service_resolver=runtime_service_resolver,
            platform_peers=_platform_peers(settings),
            settings=settings,
            stop=stop,
        ).run()
    finally:
        redis.close()
        database.dispose()


if __name__ == "__main__":
    main()

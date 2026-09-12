from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

import grpc
import httpx
from compute.state import RedisComputeStateRepository
from compute.tunnel_authority import AgentTunnelAuthority
from coordination.agent_connections import RedisAgentConnectionDirectory
from coordination.redis_client import REDIS_UNAVAILABLE_ERRORS, RedisClient, RedisSettings
from cryptography import x509
from cryptography.x509.verification import PolicyBuilder, Store
from database.settings import DatabaseApplicationName, DatabaseSettings
from gateway.settings import GatewaySettings, TunnelGatewaySettings
from identity.tunnel_certificates import service_identity_from_verified_certificate
from networking.dialer import split_host_port
from networking.tunnel_gateway import AgentTunnelGateway
from networking.tunnel_tls import TunnelCredentials, agent_certificate_request
from observability.process_logs import configure_process_logging
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.agent_connections import AgentConnectionRecord
from shared.deployment_settings import MissingDeploymentSettingError
from shared.errors import DomainError
from shared.http.agent_identity import (
    TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS,
    GatewayCertificateRequest,
    ServiceCertificateResponse,
    ServiceTunnelIdentity,
    TunnelServiceRole,
)
from sqlalchemy.exc import SQLAlchemyError

from database import DatabaseClient

LOGGER = logging.getLogger(__name__)
_DRAIN_SECONDS = 60.0
_RENEWAL_RETRY_SECONDS = 5.0


class ConnectionGatewaySettings(BaseSettings):
    control_address: str = ""
    advertised_address: str = ""
    listen_address: str = "0.0.0.0:8443"
    key_directory: Path = Path("/var/lib/lazycloud/connection-gateway")
    ready_file: Path = Path("/tmp/lazycloud-connection-gateway.ready")

    model_config = SettingsConfigDict(env_prefix="LAZYCLOUD_CONNECTION_GATEWAY_", extra="ignore")

    @model_validator(mode="after")
    def validate_addresses(self) -> ConnectionGatewaySettings:
        for name, value in (
            ("control_address", self.control_address),
            ("advertised_address", self.advertised_address),
        ):
            if not value:
                raise MissingDeploymentSettingError(
                    f"LAZYCLOUD_CONNECTION_GATEWAY_{name.upper()}",
                    purpose="the fixed API service or advertised gateway address",
                )
        self.control_address = AgentConnectionRecord.validate_gateway_address(self.control_address)
        self.advertised_address = AgentConnectionRecord.validate_gateway_address(
            self.advertised_address
        )
        self.listen_address = AgentConnectionRecord.validate_gateway_address(self.listen_address)
        advertised_host, _ = split_host_port(self.advertised_address)
        try:
            address = ip_address(advertised_host)
        except ValueError:
            pass
        else:
            if address.is_unspecified:
                raise ValueError("The advertised gateway address must identify this process")
        if not self.key_directory.is_absolute() or not self.ready_file.is_absolute():
            raise ValueError("Gateway key and readiness paths must be absolute")
        return self


@dataclass(slots=True)
class ConnectionGatewayProcess:
    settings: ConnectionGatewaySettings
    tunnel_settings: TunnelGatewaySettings
    database: DatabaseClient = field(repr=False)
    redis: RedisClient = field(repr=False)
    http: httpx.AsyncClient = field(repr=False)
    stop: asyncio.Event
    gateway_id: str = field(default_factory=lambda: str(uuid4()))
    _expires_at: datetime | None = field(default=None, init=False)

    async def run(self) -> None:
        self.settings.ready_file.unlink(missing_ok=True)
        credentials = TunnelCredentials(
            self.settings.key_directory / "private-key.pem",
            self.settings.key_directory / "certificate.json",
        )
        request = GatewayCertificateRequest(
            csr_pem=agent_certificate_request(credentials.key_path), instance_id=self.gateway_id
        )
        await self._renew(credentials, request)
        if self.stop.is_set():
            return
        gateway = AgentTunnelGateway(
            AgentTunnelAuthority(self.database, RedisComputeStateRepository(self.redis)),
            RedisAgentConnectionDirectory(self.redis),
            address=self.settings.advertised_address,
            control_address=self.settings.control_address,
            gateway_id=self.gateway_id,
        )
        server = gateway.server(credentials, listen_address=self.settings.listen_address)
        background: list[asyncio.Task[None]] = []
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(_finish_tasks, background)
            cleanup.push_async_callback(gateway.close)
            cleanup.push_async_callback(server.stop, _DRAIN_SECONDS)
            cleanup.push_async_callback(gateway.drain)
            cleanup.callback(self._stop_serving)
            await server.start()
            LOGGER.info(
                "Connection gateway listening address=%s gateway_id=%s",
                self.settings.listen_address,
                self.gateway_id,
            )
            background.extend(
                (
                    asyncio.create_task(self._maintain_certificate(credentials, request)),
                    asyncio.create_task(self._health()),
                    asyncio.create_task(self._monitor_listener(server)),
                )
            )
            stopping = asyncio.create_task(self.stop.wait())
            try:
                await asyncio.wait((*background, stopping), return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)

    async def _renew(
        self, credentials: TunnelCredentials, request: GatewayCertificateRequest
    ) -> None:
        response = await self.http.post(
            "/gateway/connections/certificate", json=request.model_dump(mode="json")
        )
        response.raise_for_status()
        issued = ServiceCertificateResponse.model_validate_json(response.content)
        expected = ServiceTunnelIdentity(
            role=TunnelServiceRole.Gateway, instance_id=self.gateway_id
        )
        if issued.identity != expected:
            raise ValueError("Certificate issuer returned a different gateway identity")
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem.encode("ascii"))
        roots = x509.load_pem_x509_certificates(issued.trust_bundle_pem.encode("ascii"))
        PolicyBuilder().store(Store(roots)).build_server_verifier(
            x509.DNSName(self.tunnel_settings.hostname)
        ).verify(certificate, [])
        if service_identity_from_verified_certificate(issued.certificate_pem) != expected:
            raise ValueError("Gateway certificate SAN does not match the requested identity")
        if (
            issued.expires_at != certificate.not_valid_after_utc
            or issued.not_before != certificate.not_valid_before_utc
        ):
            raise ValueError("Gateway certificate validity does not match the issued metadata")
        credentials.install(
            certificate_pem=issued.certificate_pem, trust_bundle_pem=issued.trust_bundle_pem
        )
        self._expires_at = issued.expires_at
        LOGGER.info("Gateway certificate installed expires_at=%s", issued.expires_at.isoformat())

    async def _maintain_certificate(
        self, credentials: TunnelCredentials, request: GatewayCertificateRequest
    ) -> None:
        while not self.stop.is_set():
            expiry = self._certificate_expiry()
            renewal_at = expiry - timedelta(seconds=TUNNEL_CERTIFICATE_RENEWAL_MARGIN_SECONDS)
            if datetime.now(UTC) >= renewal_at:
                try:
                    await self._renew(credentials, request)
                except httpx.HTTPStatusError as exc:
                    LOGGER.warning(
                        "Gateway certificate renewal failed status=%s", exc.response.status_code
                    )
                except httpx.RequestError as exc:
                    LOGGER.warning(
                        "Gateway certificate renewal unavailable error=%s", type(exc).__name__
                    )
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=_RENEWAL_RETRY_SECONDS)

    async def _health(self) -> None:
        previous: bool | None = None
        while not self.stop.is_set():
            if datetime.now(UTC) >= self._certificate_expiry():
                raise RuntimeError("Gateway certificate expired before renewal succeeded")
            database, redis = await asyncio.gather(
                _probe(self.database.ping), _probe(self.redis.ping)
            )
            if datetime.now(UTC) >= self._certificate_expiry():
                raise RuntimeError("Gateway certificate expired before renewal succeeded")
            ready = database and redis and not self.stop.is_set()
            if ready:
                self.settings.ready_file.touch(mode=0o600, exist_ok=True)
            else:
                self.settings.ready_file.unlink(missing_ok=True)
            LOGGER.debug("Gateway health database=%s redis=%s ready=%s", database, redis, ready)
            if ready != previous:
                LOGGER.info(
                    "Gateway readiness ready=%s database=%s redis=%s", ready, database, redis
                )
                previous = ready
            with suppress(TimeoutError):
                await asyncio.wait_for(self.stop.wait(), timeout=2.0)

    async def _monitor_listener(self, server: grpc.aio.Server) -> None:
        await server.wait_for_termination()
        if not self.stop.is_set():
            raise RuntimeError("Connection gateway listener stopped unexpectedly")

    def _certificate_expiry(self) -> datetime:
        if self._expires_at is None:
            raise RuntimeError("Gateway certificate has not been issued")
        return self._expires_at

    def _stop_serving(self) -> None:
        self.stop.set()
        self.settings.ready_file.unlink(missing_ok=True)
        LOGGER.info("Connection gateway draining grace_seconds=%s", _DRAIN_SECONDS)


async def _probe(check: Callable[[], bool]) -> bool:
    try:
        return await asyncio.to_thread(check)
    except (SQLAlchemyError, DomainError, *REDIS_UNAVAILABLE_ERRORS) as exc:
        LOGGER.warning("Gateway dependency probe failed error=%s", type(exc).__name__)
        return False


async def _finish_tasks(tasks: list[asyncio.Task[None]]) -> None:
    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise BaseExceptionGroup("Gateway background tasks failed", errors)


async def serve() -> None:
    settings = ConnectionGatewaySettings()
    tunnel_settings = TunnelGatewaySettings()
    public_settings = GatewaySettings()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop() -> None:
        stop.set()
        settings.ready_file.unlink(missing_ok=True)

    with ExitStack() as cleanup:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, request_stop)
            cleanup.callback(loop.remove_signal_handler, signum)
        database = DatabaseClient.from_settings(
            DatabaseSettings(application_name=DatabaseApplicationName.ConnectionGateway)
        )
        cleanup.callback(database.dispose)
        redis = RedisClient.from_settings(RedisSettings())
        cleanup.callback(redis.close)
        bootstrap_credential = tunnel_settings.gateway_bootstrap_secret.get_secret_value()
        async with httpx.AsyncClient(
            base_url=public_settings.public_http_url,
            headers={"Authorization": f"Bearer {bootstrap_credential}"},
            timeout=10.0,
            follow_redirects=False,
        ) as http:
            await ConnectionGatewayProcess(
                settings, tunnel_settings, database, redis, http, stop
            ).run()


def main() -> None:
    configure_process_logging()
    try:
        asyncio.run(serve())
    except httpx.HTTPStatusError as exc:
        LOGGER.error("Gateway certificate bootstrap failed status=%s", exc.response.status_code)
        raise SystemExit(1) from None
    except httpx.RequestError as exc:
        LOGGER.error("Gateway certificate bootstrap unavailable error=%s", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

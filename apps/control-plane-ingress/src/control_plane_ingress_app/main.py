"""The address the deployment answers on.

The control plane runs behind this rather than being it, because the address a
caller is given outlives the process that gave it. A container is handed its
control-plane origin once, at start, and holds it for its whole life; pointing it
at a replica means a pod outlives the address it was born with. Here the address
belongs to the deployment, and replicas come and go behind it.

Run as `lazycloud-control-plane-ingress`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from dataclasses import dataclass

from coordination.redis_client import RedisClient
from gateway.settings import GatewaySettings
from networking.control_plane_origin import (
    DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS,
    RedisControlPlaneOriginRepository,
    runtime_origin_for_host,
)
from networking.settings import TailnetControlSettings, TailnetRuntimeSettings
from networking.tailnet import TailnetRuntime, TailnetRuntimeMode
from networking.tailnet_control import TailscaleTailnetControl

from control_plane_ingress_app.forwarding import serve_forwarder

logger = logging.getLogger(__name__)

DEFAULT_UPSTREAM_HOST = "control-plane"
DEFAULT_CONTROL_PLANE_PORT = 9000
DEFAULT_TCP_INGRESS_PORT = 1995
DEFAULT_UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class IngressPorts:
    """A port this listens on and the identical port it forwards to.

    Deliberately the same number on both sides: the control plane builds URLs
    that name a port, and a caller reaching a different one than the control
    plane advertises produces addresses that are correct nowhere.
    """

    listen: int
    upstream: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--upstream-host", default=DEFAULT_UPSTREAM_HOST)
    parser.add_argument("--control-plane-port", type=int, default=DEFAULT_CONTROL_PLANE_PORT)
    parser.add_argument("--tcp-ingress-port", type=int, default=DEFAULT_TCP_INGRESS_PORT)
    parser.add_argument(
        "--upstream-connect-timeout-seconds",
        type=float,
        default=DEFAULT_UPSTREAM_CONNECT_TIMEOUT_SECONDS,
    )
    return parser


def start_tailnet(runtime_settings: TailnetRuntimeSettings) -> TailnetRuntime:
    """Join the tailnet under the name the deployment is known by.

    Failure is fatal rather than logged. Everything reaches the control plane
    through this address, so one that came up without it would serve nothing
    while reporting itself alive.
    """
    control_settings = TailnetControlSettings()
    runtime = TailnetRuntime(
        runtime_settings,
        auth_key_issuer=TailscaleTailnetControl(control_settings.to_control_config()),
    )
    runtime.start()
    return runtime


def publish_origin(
    origins: RedisControlPlaneOriginRepository,
    runtime: TailnetRuntime,
    configured_origin: str,
) -> str:
    origin = runtime_origin_for_host(configured_origin, runtime.self_dns_name())
    origins.publish(origin, ttl_seconds=DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS)
    return origin


async def republish_origin(
    origins: RedisControlPlaneOriginRepository,
    runtime: TailnetRuntime,
    configured_origin: str,
) -> None:
    interval = max(DEFAULT_CONTROL_PLANE_ORIGIN_TTL_SECONDS / 3, 1.0)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(publish_origin, origins, runtime, configured_origin)
        except Exception:
            logger.exception("republishing the control-plane origin failed")


async def serve(
    *,
    host: str,
    upstream_host: str,
    ports: tuple[IngressPorts, ...],
    upstream_connect_timeout_seconds: float,
    runtime: TailnetRuntime,
    origins: RedisControlPlaneOriginRepository,
    configured_origin: str,
) -> None:
    origin = await asyncio.to_thread(publish_origin, origins, runtime, configured_origin)
    logger.info("published control-plane origin: %s", origin)
    heartbeat = asyncio.create_task(republish_origin(origins, runtime, configured_origin))
    servers = [
        await serve_forwarder(
            listen_host=host,
            listen_port=port.listen,
            upstream_host=upstream_host,
            upstream_port=port.upstream,
            connect_timeout_seconds=upstream_connect_timeout_seconds,
        )
        for port in ports
    ]
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stopping.set)
    try:
        await stopping.wait()
    finally:
        heartbeat.cancel()
        for server in servers:
            server.close()
        for server in servers:
            await server.wait_closed()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    runtime_settings = TailnetRuntimeSettings()
    if runtime_settings.mode is TailnetRuntimeMode.Disabled:
        raise SystemExit(
            "the control-plane ingress is the deployment's tailnet address; "
            "set LAZYCLOUD_TAILNET_MODE=managed"
        )
    runtime = start_tailnet(runtime_settings)
    redis = RedisClient.from_settings()
    try:
        asyncio.run(
            serve(
                host=args.host,
                upstream_host=args.upstream_host,
                ports=(
                    IngressPorts(args.control_plane_port, args.control_plane_port),
                    IngressPorts(args.tcp_ingress_port, args.tcp_ingress_port),
                ),
                upstream_connect_timeout_seconds=args.upstream_connect_timeout_seconds,
                runtime=runtime,
                origins=RedisControlPlaneOriginRepository(redis),
                configured_origin=GatewaySettings().runtime_callback_http_url,
            )
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    main()

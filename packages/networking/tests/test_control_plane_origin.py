from __future__ import annotations

from networking.control_plane_origin import runtime_origin_for_host


def test_published_origin_takes_the_discovered_host_and_keeps_the_configured_port() -> None:
    """The host is discovered, the scheme and port are configured.

    Tailscale reports a name with a trailing dot, and grants a suffixed one when
    the requested name is taken. Both have already produced an origin that
    resolved nowhere, and neither fails loudly: workers sit pending and no log
    says why.
    """
    origin = runtime_origin_for_host(
        "http://lazycloud-control-plane.example.ts.net:9000",
        "lazycloud-control-plane-1.example.ts.net.",
    )

    assert origin == "http://lazycloud-control-plane-1.example.ts.net:9000"


def test_published_origin_falls_back_to_the_configured_host_without_a_device() -> None:
    """A deployment with no tailnet still has to advertise somewhere reachable."""
    assert runtime_origin_for_host("http://control-plane:9000", "") == "http://control-plane:9000"

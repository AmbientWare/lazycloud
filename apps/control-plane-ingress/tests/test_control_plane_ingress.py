from __future__ import annotations

from control_plane_ingress_app.forwarding import rotate
from networking.control_plane_origin import runtime_origin_for_host


def test_published_origin_takes_the_device_name_and_keeps_the_configured_port() -> None:
    """The host is discovered, the scheme and port are configured.

    Tailscale reports its own name with a trailing dot and grants a suffixed one
    when the requested name is taken. Both have already produced an origin that
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


def test_each_connection_starts_at_a_different_replica_and_keeps_the_rest() -> None:
    """Rotation is what spreads load; the remainder is what survives a failure.

    DNS returns every replica but in a stable order, so connecting to the first
    answer every time pins the whole deployment to one container — which is what
    happened before this existed.
    """
    addresses = [("10.0.0.1", 9000), ("10.0.0.2", 9000), ("10.0.0.3", 9000)]

    firsts = [rotate(addresses, attempt)[0] for attempt in range(len(addresses))]

    assert firsts == addresses
    assert sorted(rotate(addresses, 1)) == sorted(addresses)
    assert rotate([], 3) == []

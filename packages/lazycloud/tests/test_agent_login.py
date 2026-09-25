from urllib.parse import urlencode

import pytest
from lazycloud.session.agent_login import BrowserRequests, OpenCodeLoginUrls, callback_address
from lazycloud.session.ssh import SshSetupError


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_browser_callback_preserves_loopback_address_family(host: str) -> None:
    url = "https://auth.example/authorize?" + urlencode(
        {"redirect_uri": f"http://{host}:1455/auth/callback"}
    )
    assert callback_address(url) == (host.strip("[]"), 1455)


@pytest.mark.parametrize(
    "redirect",
    [
        "http://192.168.1.10:1455/callback",
        "http://localhost.attacker.example:1455/callback",
        "http://localhost:22/callback",
        "http://localhost:99999/callback",
        "http://user@localhost:1455/callback",
    ],
)
def test_browser_callback_refuses_unsafe_forwarding(redirect: str) -> None:
    url = "https://auth.example/authorize?" + urlencode({"redirect_uri": redirect})
    with pytest.raises(SshSetupError):
        callback_address(url)


def test_browser_request_survives_fragmentation_without_printing_login_url() -> None:
    requests = BrowserRequests("session")
    url = "https://auth.example/authorize?state=private-session-state"
    payload = b"before" + requests.prefix + url.encode() + b"\x07after"
    output = bytearray()
    urls: list[str] = []
    for byte in payload:
        emitted, opened = requests.feed(bytes([byte]))
        output.extend(emitted)
        urls.extend(opened)
    assert bytes(output) == b"beforeafter"
    assert urls == [url]
    assert requests.pending == b""


def test_opencode_login_opens_only_complete_login_urls() -> None:
    requests = OpenCodeLoginUrls()
    url = "https://auth.example/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455"
    assert requests.feed(b"Help: https://example.com\n\x1b[36mGo to: " + url[:20].encode()) == []
    assert requests.feed(url[20:].encode() + b"\x1b[0m\r\n") == [url]

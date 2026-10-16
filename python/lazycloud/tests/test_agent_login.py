import os
import pty
import shlex
from pathlib import Path
from urllib.parse import urlencode

import pytest
from lazycloud.agent_harness import AgentHarness
from lazycloud.session.agent_login import (
    BrowserRequests,
    OpenCodeLoginUrls,
    callback_address,
    login_shell_command,
)
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


def _run_login(tmp_path: Path, harness: AgentHarness) -> int:
    """Run the devbox login script on a terminal, as SSH -tt does."""
    environment = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "HOME": str(tmp_path)}
    process_id, terminal = pty.fork()
    if process_id == 0:
        script = shlex.split(login_shell_command(harness, "session"))
        os.execvpe(script[0], script, environment)
    while True:
        try:
            if not os.read(terminal, 65536):
                break
        except OSError:
            break
    _, status = os.waitpid(process_id, 0)
    os.close(terminal)
    return os.waitstatus_to_exitcode(status)


def test_claude_login_ends_once_a_new_login_is_saved(tmp_path: Path) -> None:
    # Recorded on a devbox on 2026-10-16: `claude auth login` 2.1.301 writes
    # ~/.claude/.credentials.json and keeps waiting at "Paste code here if
    # prompted >"; `claude auth status` exits 0 once signed in, 1 before.
    credentials = tmp_path / ".claude" / ".credentials.json"
    credentials.parent.mkdir()
    claude = tmp_path / "claude"
    login = (
        f"printf 'Paste code here if prompted > '; sleep 0.5; date +%N > {credentials}; "
        "exec sleep 600"
    )
    claude.write_text(
        "#!/bin/sh\n"
        f'if [ "$1 $2" = "auth status" ]; then test -s {credentials}; exit; fi\n'
        f"{login}\n"
    )
    (tmp_path / "node").write_text("#!/bin/sh\nexit 0\n")
    for tool in ("claude", "node"):
        (tmp_path / tool).chmod(0o755)

    assert _run_login(tmp_path, AgentHarness.ClaudeCode) == 0
    # Signed in already, a second login still runs and saves a new one.
    assert _run_login(tmp_path, AgentHarness.ClaudeCode) == 0

    claude.write_text(
        f'#!/bin/sh\nif [ "$1 $2" = "auth status" ]; then test -s {credentials}; exit; fi\nexit 0\n'
    )
    assert _run_login(tmp_path, AgentHarness.ClaudeCode) == 11


def test_pi_login_counts_only_a_login_saved_during_this_run(tmp_path: Path) -> None:
    # Pi keeps one entry per provider in ~/.pi/agent/auth.json (pi-coding-agent
    # 1.3.0, read on a devbox on 2026-10-16); quitting without /login leaves it.
    credentials = tmp_path / ".pi" / "agent" / "auth.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text('{"anthropic": {"type": "oauth"}}')
    pi = tmp_path / "pi"
    pi.write_text("#!/bin/sh\nexit 0\n")
    pi.chmod(0o755)
    assert _run_login(tmp_path, AgentHarness.Pi) == 11

    pi.write_text(f'#!/bin/sh\nprintf \'{{"openai": {{}}, "anthropic": {{}}}}\' > {credentials}\n')
    assert _run_login(tmp_path, AgentHarness.Pi) == 0

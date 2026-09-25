"""Run native agent login over SSH and carry browser callbacks to the container."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from urllib.parse import parse_qs, urlsplit

from lazycloud.agent_harness import AGENT_INSTALLATIONS, AgentHarness
from lazycloud.session.claude_login import CLAUDE_LOGIN_SCRIPT
from lazycloud.session.ssh import SshSetupError


@dataclass
class BrowserRequests:
    """Separate browser handoffs from terminal output, including split SSH reads."""

    nonce: str
    pending: bytes = b""

    @property
    def prefix(self) -> bytes:
        return f"\x1b]777;lazycloud-{self.nonce};".encode()

    def feed(self, data: bytes) -> tuple[bytes, list[str]]:
        self.pending += data
        output = bytearray()
        urls: list[str] = []
        while self.pending:
            start = self.pending.find(self.prefix)
            if start < 0:
                retained = next(
                    (
                        size
                        for size in range(min(len(self.pending), len(self.prefix) - 1), 0, -1)
                        if self.pending.endswith(self.prefix[:size])
                    ),
                    0,
                )
                end = len(self.pending) - retained
                output.extend(self.pending[:end])
                self.pending = self.pending[end:]
                break
            output.extend(self.pending[:start])
            self.pending = self.pending[start:]
            end = self.pending.find(b"\x07", len(self.prefix))
            if end > 16384:
                raise SshSetupError("Agent browser request exceeded the URL size limit")
            if end < 0:
                if len(self.pending) > 16384:
                    raise SshSetupError("Agent browser request exceeded the URL size limit")
                break
            try:
                urls.append(self.pending[len(self.prefix) : end].decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise SshSetupError("Agent sent an invalid browser URL") from exc
            self.pending = self.pending[end + 1 :]
        return bytes(output), urls


@dataclass
class OpenCodeLoginUrls:
    """OpenCode auth login prints its URL instead of calling a browser opener."""

    pending: bytes = b""

    def feed(self, data: bytes) -> list[str]:
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()[-16384:]
        urls: list[str] = []
        for line in lines:
            plain = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", line)
            match = re.search(rb"Go to: (https://[^\s\x1b]+)", plain)
            if match is not None:
                urls.append(match[1].decode("utf-8"))
        return urls


def callback_address(url: str) -> tuple[str, int] | None:
    """Only authorize loopback forwarding requested by an HTTPS login URL."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise ValueError
        redirects = parse_qs(parsed.query).get("redirect_uri", [])
        if not redirects:
            return None
        if len(redirects) != 1:
            raise ValueError
        redirect = urlsplit(redirects[0])
        if redirect.scheme == "https":
            return None
        if (
            redirect.scheme != "http"
            or redirect.hostname not in {"localhost", "127.0.0.1", "::1"}
            or redirect.username is not None
            or redirect.password is not None
            or redirect.port is None
            or redirect.port < 1024
        ):
            raise ValueError
        return redirect.hostname, redirect.port
    except ValueError as exc:
        raise SshSetupError(
            "Agent requested an invalid login URL or non-loopback callback"
        ) from exc


def login_shell_command(harness: AgentHarness, nonce: str) -> str:
    # Detached browser openers lose their controlling terminal. Preserve its
    # device path so the handoff still reaches the same SSH channel.
    opener = (
        f'#!/bin/sh\nprintf \'\\033]777;lazycloud-{nonce};%s\\007\' "$1" > "$LAZYCLOUD_LOGIN_TTY"\n'
    )
    installation = AGENT_INSTALLATIONS[harness]
    executable = installation.login_command[0]
    install_command = shlex.join(
        ["npm", "install", "--global", f"{installation.package}@{installation.version}"]
    )
    prerequisites = [
        (
            executable,
            f"Missing '{executable}' on the devbox.\n"
            "With Node.js 22 and npm installed, run inside the devbox:\n"
            f"  {install_command}",
        ),
        *(
            (
                dependency.command,
                f"Missing '{dependency.command}' on the devbox, required by {executable}. "
                "On Debian or Ubuntu, run as root there:\n  apt-get update && "
                f"apt-get install -y --no-install-recommends {dependency.package}",
            )
            for dependency in installation.system_dependencies
        ),
    ]
    if harness is AgentHarness.ClaudeCode:
        prerequisites.append(
            (
                "node",
                "Claude browser login requires Node.js 22 on the devbox. "
                "Install Node.js and retry.",
            )
        )
    checks = "".join(
        f"if ! command -v {shlex.quote(required)} >/dev/null 2>&1; then "
        f"printf '%s\\n' {shlex.quote(message)} >&2; agent_login_missing=1; fi; "
        for required, message in prerequisites
    )
    command = shlex.join(installation.login_command)
    if harness is AgentHarness.ClaudeCode:
        command = shlex.join(["node", "-e", CLAUDE_LOGIN_SCRIPT])
    script = (
        f"set -eu; agent_login_missing=0; {checks}"
        '[ "$agent_login_missing" -eq 0 ] || exit 127; '
        "agent_login_dir=$(mktemp -d /tmp/lazycloud-login.XXXXXXXX); "
        'agent_login_pid=; cleanup() { if [ -n "$agent_login_pid" ]; then '
        'kill -TERM "$agent_login_pid" 2>/dev/null || :; fi; '
        'rm -rf "$agent_login_dir"; }; trap cleanup EXIT; '
        "trap 'exit 130' HUP INT TERM; "
        "LAZYCLOUD_LOGIN_TTY=$(tty); export LAZYCLOUD_LOGIN_TTY; "
        f'printf %s {shlex.quote(opener)} > "$agent_login_dir/xdg-open"; '
        'chmod 700 "$agent_login_dir/xdg-open"; '
        'export BROWSER="$agent_login_dir/xdg-open"; '
        'export PATH="$agent_login_dir:$PATH"; '
        f'{command} < "$LAZYCLOUD_LOGIN_TTY" & agent_login_pid=$!; '
        'set +e; wait "$agent_login_pid"; agent_login_status=$?; '
        'agent_login_pid=; exit "$agent_login_status"'
    )
    return shlex.join(["sh", "-c", script])


@dataclass
class LoginBrowser:
    ssh: str
    config: Path
    alias: str
    control_path: Path
    open_browser: Callable[[str], bool] = webbrowser.open
    forwarded: set[tuple[str, int]] = field(default_factory=set)

    def open(self, url: str) -> None:
        address = callback_address(url)
        if address is not None and address not in self.forwarded:
            host, port = address
            # These agents advertise localhost while listening on IPv4. Bind
            # both local families explicitly so a partial port collision fails.
            target = "127.0.0.1" if host == "localhost" else host
            target = f"[{target}]" if target == "::1" else target
            bindings = ["127.0.0.1", "[::1]"] if host == "localhost" else [target]
            forwards = [
                argument
                for binding in bindings
                for argument in ("-L", f"{binding}:{port}:{target}:{port}")
            ]
            result = subprocess.run(
                [
                    self.ssh,
                    "-F",
                    str(self.config),
                    "-S",
                    str(self.control_path),
                    "-O",
                    "forward",
                    "-o",
                    "ExitOnForwardFailure=yes",
                    *forwards,
                    self.alias,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode:
                detail = result.stderr.decode("utf-8", errors="replace").strip()
                raise SshSetupError(f"Cannot forward login callback port {port}: {detail}")
            self.forwarded.add(address)
        if not self.open_browser(url):
            raise SshSetupError("Cannot open a local browser. Run this command on your desktop.")


def login_agent(config: Path, alias: str, harness: AgentHarness) -> int:
    if os.name != "posix":
        raise SshSetupError("Agent browser login requires OpenSSH on macOS, Linux, or WSL")
    ssh = shutil.which("ssh")
    if ssh is None:
        raise SshSetupError("ssh is not installed; install an OpenSSH client")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SshSetupError("Agent login requires an interactive terminal")
    nonce = token_hex(16)
    requests = BrowserRequests(nonce)
    printed_urls = OpenCodeLoginUrls() if harness is AgentHarness.OpenCode else None
    # macOS's per-user temporary directory can exceed OpenSSH's socket path limit.
    with tempfile.TemporaryDirectory(prefix="lc-login-", dir="/tmp") as directory:
        control_path = Path(directory) / "ssh"
        browser = LoginBrowser(ssh, config, alias, control_path)
        process = subprocess.Popen(
            [
                ssh,
                "-F",
                str(config),
                "-M",
                "-S",
                str(control_path),
                "-o",
                "ControlPersist=no",
                "-o",
                "ExitOnForwardFailure=yes",
                "-tt",
                alias,
                login_shell_command(harness, nonce),
            ],
            stdout=subprocess.PIPE,
        )
        try:
            if process.stdout is None:
                raise SshSetupError("Cannot read the agent login session")
            while data := os.read(process.stdout.fileno(), 65536):
                output, urls = requests.feed(data)
                if printed_urls is not None:
                    urls.extend(printed_urls.feed(output))
                sys.stdout.buffer.write(output)
                sys.stdout.buffer.flush()
                for url in urls:
                    browser.open(url)
            if requests.pending:
                raise SshSetupError("Agent browser request was interrupted")
            return process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdout is not None:
                process.stdout.close()

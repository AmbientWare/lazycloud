"""Supported coding agents and their image installation recipes."""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from urllib.parse import quote

from lazycloud._shared.enums import StringEnum
from lazycloud._shared.image_building.authoring import LinuxArchitecture


class AgentHarness(StringEnum):
    Codex = "codex"
    ClaudeCode = "claude"
    OpenCode = "opencode"
    Pi = "pi"


@dataclass(frozen=True)
class AgentSystemDependency:
    command: str
    package: str


@dataclass(frozen=True)
class AgentInstallation:
    package: str
    login_command: tuple[str, ...]
    system_dependencies: tuple[AgentSystemDependency, ...] = ()


AGENT_INSTALLATIONS = {
    AgentHarness.Codex: AgentInstallation(
        "@openai/codex", ("codex", "login"), (AgentSystemDependency("ps", "procps"),)
    ),
    AgentHarness.ClaudeCode: AgentInstallation(
        "@anthropic-ai/claude-code",
        ("claude", "auth", "login"),
        (AgentSystemDependency("script", "util-linux"),),
    ),
    AgentHarness.OpenCode: AgentInstallation("opencode-ai", ("opencode", "auth", "login")),
    AgentHarness.Pi: AgentInstallation("@earendil-works/pi-coding-agent", ("pi",)),
}

_NODE_VERSION = "22.23.3"
_NODE_RELEASES = {
    LinuxArchitecture.Amd64: (
        "x64",
        "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de",
    ),
    LinuxArchitecture.Arm64: (
        "arm64",
        "a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f",
    ),
}


class AgentVersionError(RuntimeError):
    """The npm registry did not name a coding agent's latest release."""


@cache
def latest_version(package: str) -> str:
    """The release npm tags latest for package, read once per process.

    A build installs the release current when it is deployed. Pinning it in
    the step keeps that build's cache, and a later release changes the step,
    so the next build installs that one."""
    import httpx  # containers import this module at startup; only deploys look up releases

    url = f"https://registry.npmjs.org/{quote(package, safe='@')}/latest"
    try:
        response = httpx.get(url, timeout=10, follow_redirects=True)
        response.raise_for_status()
        version = response.json()["version"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise AgentVersionError(
            f"could not read the latest release of {package} from the npm registry: {exc}"
        ) from exc
    if not isinstance(version, str) or not version or any(c.isspace() for c in version):
        raise AgentVersionError(f"the npm registry named no usable latest release of {package}")
    return version


def agent_install_commands(
    harnesses: Iterable[AgentHarness], architecture: LinuxArchitecture
) -> list[str]:
    selected = tuple(dict.fromkeys(AgentHarness(item) for item in harnesses))
    if not selected:
        return []
    node_arch, checksum = _NODE_RELEASES[architecture]
    archive = f"node-v{_NODE_VERSION}-linux-{node_arch}.tar.xz"
    packages = [
        shlex.quote(
            f"{AGENT_INSTALLATIONS[item].package}@{latest_version(AGENT_INSTALLATIONS[item].package)}"
        )
        for item in selected
    ]
    system_packages = shlex.join(
        dict.fromkeys(
            dependency.package
            for item in selected
            for dependency in AGENT_INSTALLATIONS[item].system_dependencies
        )
    )
    return [
        "command -v apt-get >/dev/null || "
        "{ echo 'Agent harness installation requires a Debian or Ubuntu image' >&2; exit 1; }; "
        "apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        f"ca-certificates curl git xz-utils libstdc++6 libatomic1 {system_packages} "
        "&& rm -rf /var/lib/apt/lists/*",
        "set -eu; agent_install_dir=$(mktemp -d); "
        "trap 'rm -rf \"$agent_install_dir\"' EXIT; "
        "curl --fail --silent --show-error --location "
        f"https://nodejs.org/dist/v{_NODE_VERSION}/{archive} "
        '-o "$agent_install_dir/node.tar.xz"; '
        f'echo "{checksum}  $agent_install_dir/node.tar.xz" | sha256sum --check; '
        'tar -xJf "$agent_install_dir/node.tar.xz" -C /usr/local --strip-components=1',
        f"npm install --global --no-audit --no-fund {' '.join(packages)} "
        "&& npm cache clean --force",
    ]


__all__ = ["AgentHarness", "AgentVersionError"]

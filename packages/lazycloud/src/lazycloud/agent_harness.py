"""Supported coding agents and their image installation recipes."""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass

from shared.enums import StringEnum
from shared.image_building.authoring import LinuxArchitecture


class AgentHarness(StringEnum):
    Codex = "codex"
    ClaudeCode = "claude-code"
    OpenCode = "opencode"
    Pi = "pi"


@dataclass(frozen=True)
class AgentSystemDependency:
    command: str
    package: str


@dataclass(frozen=True)
class AgentInstallation:
    package: str
    version: str
    login_command: tuple[str, ...]
    system_dependencies: tuple[AgentSystemDependency, ...] = ()


AGENT_INSTALLATIONS = {
    AgentHarness.Codex: AgentInstallation(
        "@openai/codex", "0.156.1", ("codex", "login"), (AgentSystemDependency("ps", "procps"),)
    ),
    AgentHarness.ClaudeCode: AgentInstallation(
        "@anthropic-ai/claude-code", "2.1.282", ("claude", "auth", "login")
    ),
    AgentHarness.OpenCode: AgentInstallation(
        "opencode-ai", "1.18.32", ("opencode", "auth", "login")
    ),
    AgentHarness.Pi: AgentInstallation("@earendil-works/pi-coding-agent", "0.87.1", ("pi",)),
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


def agent_install_commands(
    harnesses: Iterable[AgentHarness], architecture: LinuxArchitecture
) -> list[str]:
    selected = tuple(dict.fromkeys(AgentHarness(item) for item in harnesses))
    if not selected:
        return []
    node_arch, checksum = _NODE_RELEASES[architecture]
    archive = f"node-v{_NODE_VERSION}-linux-{node_arch}.tar.xz"
    packages = [
        shlex.quote(f"{AGENT_INSTALLATIONS[item].package}@{AGENT_INSTALLATIONS[item].version}")
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


__all__ = ["AgentHarness"]

from __future__ import annotations

import base64
from urllib.parse import urlparse

from foundation.shell import shell_quote
from pydantic import field_validator
from shared.contracts import ContractModel


class MachineBootstrapConfig(ContractModel):
    registration_token: str
    machine_id: str
    gateway_url: str
    install_nvidia_runtime: bool = True

    @field_validator("gateway_url")
    @classmethod
    def validate_gateway_url(cls, value: str) -> str:
        gateway_url = value.strip().rstrip("/")
        parsed = urlparse(gateway_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "gateway URL must be an HTTPS origin without credentials, path, query, or fragment"
            )
        return gateway_url


def machine_bootstrap_user_data(config: MachineBootstrapConfig) -> str:
    gateway_url = config.gateway_url
    lines = [
        "#!/bin/sh",
        "set -eu",
    ]
    if config.install_nvidia_runtime:
        lines.extend(
            [
                "if command -v nvidia-ctk >/dev/null 2>&1; then",
                "  nvidia-ctk configure --runtime=docker || true",
                "  systemctl restart docker || true",
                "fi",
            ]
        )
    lines.extend(
        [
            "curl -fsSL --retry 10 --retry-all-errors --retry-delay 5 --retry-max-time 300 "
            f"{shell_quote(f'{gateway_url}/install/agent')} | sh -s -- \\",
            f"  --gateway {shell_quote(gateway_url)} \\",
            f"  --join-token {shell_quote(config.registration_token)} \\",
            f"  --machine-fingerprint {shell_quote(config.machine_id)} \\",
            f"  --hostname {shell_quote(config.machine_id)} \\",
            "  --executor container \\",
            "  --install-docker auto \\",
            "  --install-newt auto \\",
            "  --background",
        ]
    )
    return "\n".join(lines) + "\n"


def machine_bootstrap_user_data_base64(config: MachineBootstrapConfig) -> str:
    return base64.b64encode(machine_bootstrap_user_data(config).encode()).decode()


__all__ = [
    "MachineBootstrapConfig",
    "machine_bootstrap_user_data",
    "machine_bootstrap_user_data_base64",
]

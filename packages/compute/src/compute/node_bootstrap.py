"""The node bootstrap script every managed pool runs, and the seam a provider fills.

A machine launched into a managed pool has to reach the control plane before it
has an agent, an identity, or a network. Almost none of that is
provider-specific, and the part that is comes first: how the node learns its own
id, how it proves that identity, and which flags the agent needs to verify the
proof. So the script lives here and the provider supplies a shell fragment. The
alternative was a copy per cloud, which is how the script would drift.

Everything after identity belongs to the published agent installer, which this
script downloads and runs. It installs the container runtime, the agent,
and the agent's systemd unit.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator
from shared.app_identity import (
    AGENT_NAME,
    AGENT_STATE_DIR,
)
from shared.contracts import ContractModel
from shared.provider_config import ProviderKind
from shared.urls import normalize_http_origin

AGENT_BIN_PATH = f"/usr/local/bin/{AGENT_NAME}"
AGENT_SERVICE_NAME = f"{AGENT_NAME}.service"

_PROVIDER_IDENTITY_MARKER = "# __PROVIDER_IDENTITY__"
_SENTINEL_PATTERN = re.compile(r"__[A-Z0-9_]+__")
_ENTRY_POINT = "bootstrap_main"

# What a provider fragment must define. The script calls each of these by name,
# so a fragment missing one produces a boot that fails on a real machine with an
# opaque "command not found"; assert them at build time instead.
_REQUIRED_PROVIDER_SYMBOLS = (
    "resolve_node_identity()",
    "report_identity_fields()",
    "node_fingerprint()",
    "node_hostname()",
    "PROVIDER_INSTALL_FLAGS=(",
)

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_ENROLLMENT_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"


class NodeBootstrapError(ValueError):
    """A bootstrap script could not be assembled."""


def validate_agent_binary_url(value: str) -> str:
    """The artifact a node downloads before it can verify anything else.

    Its digest is pinned separately, so the transport only has to prevent the
    URL from carrying credentials into user-data.
    """
    url = value.strip()
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("agent artifact URL must be an HTTPS URL without credentials")
    return url


class NodeBootstrapSettings(ContractModel):
    """Everything a booting node needs that no provider owns.

    `control_plane_url` is the public origin for enrollment and agent management.
    """

    control_plane_url: str
    enrollment_request_id: str = Field(pattern=_ENROLLMENT_PATTERN)
    agent_binary_url: str
    agent_sha256: str = Field(pattern=_DIGEST_PATTERN)
    gpu_count: int = Field(default=0, ge=0, le=8)

    @field_validator("control_plane_url")
    @classmethod
    def validate_control_plane_url(cls, value: str) -> str:
        return normalize_http_origin(value, field_name="control-plane URL")

    @field_validator("agent_binary_url")
    @classmethod
    def normalize_agent_binary_url(cls, value: str) -> str:
        return validate_agent_binary_url(value)


@dataclass(frozen=True, slots=True)
class NodeBootstrapProfile:
    """The provider half of the bootstrap, as shell.

    `identity_shell` must define, in POSIX-compatible bash:

    - `resolve_node_identity()` — populate whatever the other three read. Runs
      before any network call, so it may only use provider metadata reachable
      without one (a link-local metadata service, a mounted attestation).
    - `report_identity_fields()` — print the provider's JSON fields for a
      bootstrap report, each preceded by a comma, including whatever proves the
      node's identity to the control plane.
    - `node_fingerprint()` / `node_hostname()` — print the machine fingerprint
      and hostname the agent enrols with.
    - `PROVIDER_INSTALL_FLAGS=(...)` — extra `install-service` arguments.

    A fragment may declare its own `__SENTINEL__` values through `values`; they
    are substituted in the same pass as the generic ones.
    """

    provider: str
    identity_shell: str
    values: Mapping[str, str] = field(default_factory=dict[str, str])


_BOOTSTRAP_SCRIPT_TEMPLATE = """#!/bin/bash
set +x
set -Eeuo pipefail

exec > >(tee -a /var/log/lazycloud-bootstrap.log) 2>&1

CONTROL_PLANE_URL=__CONTROL_PLANE_URL__
ENROLLMENT_REQUEST_ID=__ENROLLMENT_REQUEST_ID__
AGENT_SHA256=__AGENT_SHA256__
AGENT_BINARY_URL=__AGENT_BINARY_URL__
GPU_COUNT=__GPU_COUNT__
AGENT_STATE_DIR=__AGENT_STATE_DIR__

STEP=identity

bootstrap_failed() {
  status=$?
  trap - ERR
  reason=unknown
  case "$STEP" in
    identity) reason=provider_identity_failed ;;
    install) reason=agent_enrollment_failed ;;
  esac
  report_failure "$reason"
  echo "worker bootstrap failed during ${STEP}; the control plane reclaims this instance" >&2
  exit "$status"
}
trap bootstrap_failed ERR

bootstrap_error() {
  echo "error: $1" >&2
  return 1
}

# __PROVIDER_IDENTITY__

# The only report this script still owns. Everything after the installer hands
# off is the agent's to report, but a node that dies before the agent exists
# has no other voice, and the control plane would see nothing but a deadline.
report_failure() {
  payload="{\\"enrollment_request_id\\":\\"${ENROLLMENT_REQUEST_ID}\\""
  payload="${payload}$(report_identity_fields)"
  payload="${payload},\\"failure_reason\\":\\"$1\\"}"
  if curl -fsS --retry 5 --retry-all-errors --retry-delay 2 -X POST \\
    -H 'Content-Type: application/json' \\
    --data-binary @- \\
    "${CONTROL_PLANE_URL}/gateway/provider-nodes/bootstrap-failure" <<< "$payload" >/dev/null; then
    rm -f "$AGENT_STATE_DIR/provider-bootstrap-token"
  fi
}

bootstrap_main() {
  install -d -m 0700 "$AGENT_STATE_DIR"

  STEP=identity
  resolve_node_identity

  STEP=install
  installer=/tmp/lazycloud-agent-install.sh
  curl -fsS --retry 5 --retry-all-errors --retry-delay 2 \\
    "${CONTROL_PLANE_URL}/install/agent" -o "$installer"
  sh "$installer" \\
    --gateway "$CONTROL_PLANE_URL" \\
    --agent-url "$AGENT_BINARY_URL" \\
    --agent-sha256 "$AGENT_SHA256" \\
    --provider-enrollment-request "$ENROLLMENT_REQUEST_ID" \\
    "${PROVIDER_INSTALL_FLAGS[@]}" \\
    --machine-fingerprint "$(node_fingerprint)" \\
    --hostname "$(node_hostname)" \\
    --executor container \\
    --max-gpus "$GPU_COUNT" \\
    --state-dir "$AGENT_STATE_DIR"
}

bootstrap_main
"""


def node_bootstrap_script(
    settings: NodeBootstrapSettings,
    profile: NodeBootstrapProfile,
) -> str:
    """Assemble the script a node runs from user-data.

    The provider fragment is spliced before substitution so that it may carry
    its own sentinels. A single pass replaces `__NAME__` tokens with quoted
    values without interpreting their contents or the shell's brace expansions.
    """
    missing = [
        symbol for symbol in _REQUIRED_PROVIDER_SYMBOLS if symbol not in profile.identity_shell
    ]
    if missing:
        msg = f"{profile.provider} bootstrap fragment does not define: {', '.join(missing)}"
        raise NodeBootstrapError(msg)

    script = _BOOTSTRAP_SCRIPT_TEMPLATE.replace(
        _PROVIDER_IDENTITY_MARKER,
        profile.identity_shell.strip("\n"),
    )
    values = {
        "__CONTROL_PLANE_URL__": settings.control_plane_url,
        "__ENROLLMENT_REQUEST_ID__": settings.enrollment_request_id,
        "__AGENT_SHA256__": settings.agent_sha256,
        "__AGENT_BINARY_URL__": settings.agent_binary_url,
        "__GPU_COUNT__": str(settings.gpu_count),
        "__AGENT_STATE_DIR__": AGENT_STATE_DIR,
        **dict(profile.values),
    }
    unresolved = sorted(set(_SENTINEL_PATTERN.findall(script)) - values.keys())
    if unresolved:
        msg = f"node bootstrap script has unresolved placeholders: {', '.join(unresolved)}"
        raise NodeBootstrapError(msg)
    script = _SENTINEL_PATTERN.sub(lambda match: shlex.quote(values[match.group(0)]), script)
    if script.rstrip("\n").rsplit("\n", 1)[-1] != _ENTRY_POINT:
        msg = f"node bootstrap script must end by calling {_ENTRY_POINT}"
        raise NodeBootstrapError(msg)
    return script


def provider_bootstrap_script(
    settings: NodeBootstrapSettings,
    *,
    provider: ProviderKind,
    region: str,
    launch_id: str,
    bootstrap_token: SecretStr,
    instance_identity_shell: str,
    values: Mapping[str, str],
) -> str:
    if provider is ProviderKind.Aws:
        raise NodeBootstrapError("AWS requires signed instance identity")
    if urlparse(settings.control_plane_url).scheme != "https":
        raise NodeBootstrapError("provider bootstrap credentials require an HTTPS origin")
    if "resolve_provider_instance_id()" not in instance_identity_shell:
        raise NodeBootstrapError("provider identity must resolve its instance ID")
    identity_values = {
        "__BOOTSTRAP_PROVIDER__": provider.value,
        "__BOOTSTRAP_REGION__": region,
        "__BOOTSTRAP_LAUNCH_ID__": launch_id,
        "__BOOTSTRAP_TOKEN__": bootstrap_token.get_secret_value(),
    }
    if identity_values.keys() & values.keys():
        raise NodeBootstrapError("provider identity overrides shared enrollment values")
    return node_bootstrap_script(
        settings,
        NodeBootstrapProfile(
            provider=provider.value,
            identity_shell=instance_identity_shell + _PROVIDER_BOOTSTRAP_IDENTITY,
            values={**identity_values, **values},
        ),
    )


_PROVIDER_BOOTSTRAP_IDENTITY = r"""
resolve_node_identity() {
  umask 077
  PROVIDER_INSTANCE_ID=$(resolve_provider_instance_id)
  test -n "$PROVIDER_INSTANCE_ID"
  printf '%s' "$PROVIDER_INSTANCE_ID" > "$AGENT_STATE_DIR/provider-instance-id"
  printf '%s' __BOOTSTRAP_REGION__ > "$AGENT_STATE_DIR/provider-region"
  if [ -f "$AGENT_STATE_DIR/provider-launch-id" ]; then
    [ "$(< "$AGENT_STATE_DIR/provider-launch-id")" = __BOOTSTRAP_LAUNCH_ID__ ]
  else
    printf '%s' __BOOTSTRAP_LAUNCH_ID__ > "$AGENT_STATE_DIR/provider-launch-id"
  fi
  if [ ! -f "$AGENT_STATE_DIR/provider-node-token" ]; then
    credential_tmp=$(mktemp "$AGENT_STATE_DIR/.provider-node-token.XXXXXX")
    head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n' > "$credential_tmp"
    sync -f "$credential_tmp"
    printf '%s' __BOOTSTRAP_TOKEN__ > "$AGENT_STATE_DIR/provider-bootstrap-token"
    if ! ln "$credential_tmp" "$AGENT_STATE_DIR/provider-node-token"; then
      [ -f "$AGENT_STATE_DIR/provider-node-token" ]
    fi
    rm -f "$credential_tmp"
    sync -f "$AGENT_STATE_DIR"
  fi
}
report_identity_fields() {
  printf ',"provider":"%s","region":"%s"' __BOOTSTRAP_PROVIDER__ __BOOTSTRAP_REGION__
  printf ',"provider_instance_id":"%s"' "$PROVIDER_INSTANCE_ID"
  printf ',"identity_proof_url":"provider-bootstrap"'
  printf ',"launch_id":"%s"' "$(< "$AGENT_STATE_DIR/provider-launch-id")"
  printf ',"node_agent_token":"%s"' "$(< "$AGENT_STATE_DIR/provider-node-token")"
  if [ -f "$AGENT_STATE_DIR/provider-bootstrap-token" ]; then
    printf ',"bootstrap_token":"%s"' "$(< "$AGENT_STATE_DIR/provider-bootstrap-token")"
  fi
}
node_fingerprint() { printf '%s:%s' __BOOTSTRAP_PROVIDER__ "$PROVIDER_INSTANCE_ID"; }
node_hostname() { hostname; }
PROVIDER_INSTALL_FLAGS=(
  --provider __BOOTSTRAP_PROVIDER__ --provider-instance-identity provider-bootstrap
)
"""


__all__ = [
    "AGENT_BIN_PATH",
    "NodeBootstrapError",
    "NodeBootstrapProfile",
    "NodeBootstrapSettings",
    "node_bootstrap_script",
    "provider_bootstrap_script",
    "validate_agent_binary_url",
]

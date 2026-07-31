"""The node bootstrap script every managed pool runs, and the seam a provider fills.

A machine launched into a managed pool has to reach the control plane before it
has an agent, an identity, or a network. Most of what it does to get there is
the same everywhere: install a container runtime, install Tailscale, download
and verify the agent, report progress, and hand off to the agent's own service.
Only three things are provider-specific — how the node learns its own id, how it
proves that identity, and which flags the agent needs to verify the proof.

So the script lives here and the provider supplies a shell fragment. The
alternative was a copy per cloud, which is how the script would drift.

The node joins the tailnet before its first control-plane call. That ordering is
the point of the module: enrolment used to require a publicly reachable origin,
which meant Tailscale Funnel carried the boot path and timed out under the image
transfer that immediately followed it. With a pool-scoped key already in
user-data, the node is a tailnet peer by the time it reports `booting`, and the
control plane is addressed as a peer for the rest of its life.

The bootstrap runs `tailscaled` at the *agent's* state directory and socket, and
stops it before handing off. Two daemons would contend for the same state file
and TUN device, and the agent's runtime has no way to adopt one it did not
spawn. `tailscale up --reset` persists `WantRunning=true`, so when the agent
starts its own daemon from that state it resumes the same node key rather than
logging in twice.
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
    AGENT_TAILNET_DIR_NAME,
    TAILSCALED_SOCKET_NAME,
    TAILSCALED_STATE_NAME,
)
from shared.contracts import ContractModel
from shared.tailscale_install import TAILSCALE_AMD64_SHA256, TAILSCALE_INSTALL_VERSION
from shared.urls import normalize_http_origin

# The agent derives these from the `--state-dir` this script passes it. Every
# component of the derivation is named once, in `shared.app_identity`, because
# a single character of drift here makes the agent spawn a rival tailscaled
# against the same state file rather than resuming this one's session.
AGENT_BIN_PATH = f"/usr/local/bin/{AGENT_NAME}"
TAILNET_STATE_DIR = f"{AGENT_STATE_DIR}/{AGENT_TAILNET_DIR_NAME}"
TAILNET_SOCKET_PATH = f"{TAILNET_STATE_DIR}/{TAILSCALED_SOCKET_NAME}"
TAILNET_STATE_FILE = f"{TAILNET_STATE_DIR}/{TAILSCALED_STATE_NAME}"

# Resolved through PATH, matching the agent's own defaults, so a node that
# passes `tailscale_ready` is running the binaries the agent will later find.
TAILSCALE_BINARY = "tailscale"
TAILSCALED_BINARY = "tailscaled"

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
_WORKER_IMAGE_PATTERN = r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$"


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

    `control_plane_url` is a tailnet origin for a managed pool. It is validated
    only as an origin here because whether a given host is a peer is a fact
    about the tailnet, not about the string; the node proves it by resolving the
    peer during `tailnet_join` and refusing to continue if it cannot.
    """

    control_plane_url: str
    enrollment_request_id: str = Field(pattern=_ENROLLMENT_PATTERN)
    agent_binary_url: str
    agent_sha256: str = Field(pattern=_DIGEST_PATTERN)
    worker_image_digest: str = Field(pattern=_WORKER_IMAGE_PATTERN)
    gpu_count: int = Field(default=0, ge=0, le=8)
    tailnet_auth_key: SecretStr
    tailscale_version: str = TAILSCALE_INSTALL_VERSION
    tailscale_sha256: str = TAILSCALE_AMD64_SHA256

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
set -Eeuo pipefail

CONTROL_PLANE_URL=__CONTROL_PLANE_URL__
ENROLLMENT_REQUEST_ID=__ENROLLMENT_REQUEST_ID__
AGENT_SHA256=__AGENT_SHA256__
AGENT_BINARY_URL=__AGENT_BINARY_URL__
WORKER_IMAGE_DIGEST=__WORKER_IMAGE_DIGEST__
GPU_COUNT=__GPU_COUNT__
TAILSCALE_VERSION=__TAILSCALE_VERSION__
TAILSCALE_SHA256=__TAILSCALE_SHA256__
TAILNET_AUTH_KEY=__TAILNET_AUTH_KEY__
AGENT_BIN=__AGENT_BIN__
AGENT_STATE_DIR=__AGENT_STATE_DIR__
TAILNET_STATE_DIR=__TAILNET_STATE_DIR__
TAILNET_STATE_FILE=__TAILNET_STATE_FILE__
TAILNET_SOCKET=__TAILNET_SOCKET__
TAILSCALE_BIN=__TAILSCALE_BIN__
TAILSCALED_BIN=__TAILSCALED_BIN__

STEP=identity
TAILSCALED_PID=""
CONTROL_PLANE_HOST=""
CONTROL_PLANE_PORT=""
# Pins the control plane's dial address to its tailnet peer address while the
# request keeps the name it was configured with, mirroring how every other
# internal client on the platform reaches a peer.
CURL_RESOLVE=()

bootstrap_failed() {
  status=$?
  trap - ERR
  reason=unknown
  case "$STEP" in
    identity) reason=provider_identity_failed ;;
    tailscale) reason=runtime_install_failed ;;
    tailnet) reason=network_join_failed ;;
    docker) reason=runtime_install_failed ;;
    agent) reason=agent_download_failed ;;
    service) reason=agent_enrollment_failed ;;
  esac
  report_failure "$reason"
  echo "worker bootstrap failed during ${STEP}; leaving instance available for inspection" >&2
  exit "$status"
}
trap bootstrap_failed ERR

bootstrap_error() {
  echo "error: $1" >&2
  return 1
}

# __PROVIDER_IDENTITY__

# Bootstrap reports carry whatever the provider uses to prove this node's
# identity and never abort the boot flow.
report() {
  endpoint="$1"
  field="$2"
  value="$3"
  payload="{\\"enrollment_request_id\\":\\"${ENROLLMENT_REQUEST_ID}\\""
  payload="${payload}$(report_identity_fields)"
  payload="${payload},\\"${field}\\":\\"${value}\\"}"
  curl -fsS "${CURL_RESOLVE[@]}" --retry 5 --retry-all-errors --retry-delay 2 -X POST \\
    -H 'Content-Type: application/json' \\
    --data "$payload" \\
    "${CONTROL_PLANE_URL}/gateway/provider-nodes/${endpoint}" >/dev/null
}

report_phase() {
  report bootstrap-phase phase "$1" || true
}

report_failure() {
  report bootstrap-failure failure_reason "$1" || true
}

ts() {
  "$TAILSCALE_BIN" --socket="$TAILNET_SOCKET" "$@"
}

parse_control_plane_origin() {
  scheme="${CONTROL_PLANE_URL%%://*}"
  authority="${CONTROL_PLANE_URL#*://}"
  authority="${authority%%/*}"
  case "$authority" in
    *:*)
      CONTROL_PLANE_HOST="${authority%:*}"
      CONTROL_PLANE_PORT="${authority##*:}"
      ;;
    *)
      CONTROL_PLANE_HOST="$authority"
      if [ "$scheme" = "https" ]; then
        CONTROL_PLANE_PORT=443
      else
        CONTROL_PLANE_PORT=80
      fi
      ;;
  esac
  if [ -z "$CONTROL_PLANE_HOST" ]; then
    bootstrap_error 'control-plane URL has no host'
  fi
}

docker_ready() {
  docker info >/dev/null 2>&1
}

tailscale_ready() {
  command -v "$TAILSCALE_BIN" >/dev/null 2>&1 && \\
    command -v "$TAILSCALED_BIN" >/dev/null 2>&1 && \\
    [ "$("$TAILSCALE_BIN" version 2>/dev/null | sed -n 1p)" = "$TAILSCALE_VERSION" ] && \\
    [ "$("$TAILSCALED_BIN" --version 2>/dev/null | sed -n 1p)" = "$TAILSCALE_VERSION" ]
}

agent_ready() {
  [ -x "$AGENT_BIN" ] && \\
    [ "$(sha256sum "$AGENT_BIN" 2>/dev/null | awk '{print $1}')" = "$AGENT_SHA256" ]
}

ensure_docker() {
  if docker_ready; then
    return
  fi
  if ! command -v docker >/dev/null 2>&1; then
    if ! command -v dnf >/dev/null 2>&1; then
      bootstrap_error 'no supported package manager for the container runtime'
    fi
    dnf install -y docker
  fi
  systemctl enable --now docker
  for _ in {1..30}; do
    if docker_ready; then
      return
    fi
    sleep 2
  done
  bootstrap_error 'Docker daemon did not become ready'
}

ensure_tailscale() {
  if tailscale_ready; then
    return
  fi
  archive=$(mktemp)
  extracted=$(mktemp -d)
  curl -fsSL --retry 5 --retry-delay 2 \\
    "https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz" \\
    -o "$archive"
  if [ "$(sha256sum "$archive" | awk '{print $1}')" != "$TAILSCALE_SHA256" ]; then
    rm -rf "$archive" "$extracted"
    bootstrap_error 'Tailscale archive SHA-256 mismatch'
  fi
  tar -xzf "$archive" -C "$extracted"
  release_dir="${extracted}/tailscale_${TAILSCALE_VERSION}_amd64"
  install -m 0755 "$release_dir/tailscale" /usr/local/bin/tailscale
  install -m 0755 "$release_dir/tailscaled" /usr/local/bin/tailscaled
  rm -rf "$archive" "$extracted"
  if ! tailscale_ready; then
    bootstrap_error 'Tailscale failed its pinned version readiness check'
  fi
}

ensure_agent() {
  if agent_ready; then
    return
  fi
  agent_download=$(mktemp "${AGENT_BIN}.download.XXXXXX")
  curl -fsSL --retry 5 --retry-delay 2 "$AGENT_BINARY_URL" -o "$agent_download"
  if [ "$(sha256sum "$agent_download" | awk '{print $1}')" != "$AGENT_SHA256" ]; then
    rm -f "$agent_download"
    bootstrap_error 'agent artifact SHA-256 mismatch'
  fi
  chmod 0755 "$agent_download"
  mv -f "$agent_download" "$AGENT_BIN"
}

# Joins the tailnet at the agent's own state directory and socket so the agent
# resumes this session instead of starting a rival daemon against the same
# state. The key reaches tailscale through a 0600 file rather than a command
# line, which is world-readable through /proc.
tailnet_join() {
  parse_control_plane_origin
  install -d -m 0700 "$TAILNET_STATE_DIR"
  key_file="${TAILNET_STATE_DIR}/bootstrap.key"
  (umask 077 && printf '%s\\n' "$TAILNET_AUTH_KEY" >"$key_file")
  "$TAILSCALED_BIN" \\
    --state="$TAILNET_STATE_FILE" \\
    --socket="$TAILNET_SOCKET" \\
    >>"${TAILNET_STATE_DIR}/tailscaled.log" 2>&1 &
  TAILSCALED_PID=$!
  daemon_ready=""
  for _ in {1..60}; do
    if ! kill -0 "$TAILSCALED_PID" 2>/dev/null; then
      rm -f "$key_file"
      bootstrap_error 'tailscaled exited before it finished starting'
    fi
    if ts status --json >/dev/null 2>&1; then
      daemon_ready=1
      break
    fi
    sleep 1
  done
  if [ -z "$daemon_ready" ]; then
    rm -f "$key_file"
    bootstrap_error 'tailscaled did not finish loading its identity state'
  fi
  if ! ts up --auth-key="file:${key_file}" \\
    --hostname="bootstrap-$(node_fingerprint)" \\
    --accept-dns=false \\
    --accept-routes=false \\
    --reset; then
    rm -f "$key_file"
    bootstrap_error 'tailnet join was refused'
  fi
  rm -f "$key_file"
  control_plane_address="$(ts ip -4 "$CONTROL_PLANE_HOST" 2>/dev/null | sed -n 1p)"
  if [ -z "$control_plane_address" ]; then
    control_plane_address="$(ts ip -4 "${CONTROL_PLANE_HOST%%.*}" 2>/dev/null | sed -n 1p)"
  fi
  if [ -z "$control_plane_address" ]; then
    bootstrap_error "control plane ${CONTROL_PLANE_HOST} is not a reachable tailnet peer"
  fi
  CURL_RESOLVE=(--resolve "${CONTROL_PLANE_HOST}:${CONTROL_PLANE_PORT}:${control_plane_address}")
}

# The agent owns the tailnet from here. Leaving this daemon running would make
# it a second owner of the same state file and TUN device.
tailnet_stop() {
  if [ -z "$TAILSCALED_PID" ]; then
    return
  fi
  kill "$TAILSCALED_PID" 2>/dev/null || true
  (sleep 30 && kill -9 "$TAILSCALED_PID" 2>/dev/null) &
  watchdog=$!
  wait "$TAILSCALED_PID" 2>/dev/null || true
  kill "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
  TAILSCALED_PID=""
}

bootstrap_main() {
  install -d -m 0700 "$AGENT_STATE_DIR"

  STEP=identity
  resolve_node_identity

  # Tailscale before the first report: every report travels over the tailnet.
  STEP=tailscale
  ensure_tailscale
  STEP=tailnet
  tailnet_join

  report_phase booting

  STEP=docker
  ensure_docker
  STEP=agent
  ensure_agent

  report_phase joining
  STEP=tailnet
  tailnet_stop

  # The agent must outlive cloud-init. Running it as a cloud-init child leaves
  # the machine with no agent once that script module exits: it enrolls once,
  # registers a worker, then disappears, so the worker never leaves `pending`.
  #
  # The agent installs its own unit. Writing one here too would make this script
  # a second owner of the same file, and the two silently disagreed: a machine
  # was found running the agent's unit while this script claimed a different
  # restart policy, so a fix applied here never reached any machine.
  STEP=service
  "$AGENT_BIN" install-service \\
    --gateway "$CONTROL_PLANE_URL" \\
    --provider-enrollment-request "$ENROLLMENT_REQUEST_ID" \\
    "${PROVIDER_INSTALL_FLAGS[@]}" \\
    --machine-fingerprint "$(node_fingerprint)" \\
    --hostname "$(node_hostname)" \\
    --executor container \\
    --worker-image "$WORKER_IMAGE_DIGEST" \\
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
    its own sentinels, and substitution is a plain replace of `__NAME__` tokens
    with `shlex.quote`d values — the script contains brace expansions and shell
    parameter expansions that any format-string mechanism would eat.
    """
    auth_key = settings.tailnet_auth_key.get_secret_value()
    if not auth_key.strip():
        msg = "a node bootstrap script requires a tailnet auth key"
        raise NodeBootstrapError(msg)
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
        "__WORKER_IMAGE_DIGEST__": settings.worker_image_digest,
        "__GPU_COUNT__": str(settings.gpu_count),
        "__TAILSCALE_VERSION__": settings.tailscale_version,
        "__TAILSCALE_SHA256__": settings.tailscale_sha256,
        "__TAILNET_AUTH_KEY__": auth_key,
        "__AGENT_BIN__": AGENT_BIN_PATH,
        "__AGENT_STATE_DIR__": AGENT_STATE_DIR,
        "__TAILNET_STATE_DIR__": TAILNET_STATE_DIR,
        "__TAILNET_STATE_FILE__": TAILNET_STATE_FILE,
        "__TAILNET_SOCKET__": TAILNET_SOCKET_PATH,
        "__TAILSCALE_BIN__": TAILSCALE_BINARY,
        "__TAILSCALED_BIN__": TAILSCALED_BINARY,
        **dict(profile.values),
    }
    for placeholder, value in values.items():
        script = script.replace(placeholder, shlex.quote(value))

    unresolved = sorted(set(_SENTINEL_PATTERN.findall(script)))
    if unresolved:
        msg = f"node bootstrap script has unresolved placeholders: {', '.join(unresolved)}"
        raise NodeBootstrapError(msg)
    if script.rstrip("\n").rsplit("\n", 1)[-1] != _ENTRY_POINT:
        msg = f"node bootstrap script must end by calling {_ENTRY_POINT}"
        raise NodeBootstrapError(msg)
    return script


__all__ = [
    "AGENT_BIN_PATH",
    "TAILNET_SOCKET_PATH",
    "TAILNET_STATE_DIR",
    "TAILNET_STATE_FILE",
    "TAILSCALED_BINARY",
    "TAILSCALE_BINARY",
    "NodeBootstrapError",
    "NodeBootstrapProfile",
    "NodeBootstrapSettings",
    "node_bootstrap_script",
    "validate_agent_binary_url",
]

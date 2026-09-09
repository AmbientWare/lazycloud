from __future__ import annotations

import hashlib
import math
import posixpath
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse

from compute.telemetry import redact_telemetry_line
from pydantic import Field, JsonValue, field_validator
from shared.app_identity import (
    ADMIN_CLI_NAME,
    AGENT_CONTAINER_DATA_PATH,
    AGENT_CONTAINER_LOG_PATH,
    AGENT_CONTAINER_TMP_PATH,
    AGENT_NAME,
    CONTAINER_WORKER_PROCESS_NAME,
    HOME_DIR,
    NAME,
)
from shared.capacity import CAPACITY_OWNER_ID_PATTERN
from shared.compute_enrollment import (
    AgentCapacityState,
    AgentWorkerSlotStatus,
    PreflightSeverity,
)
from shared.compute_policy import LAZYCLOUD_MACHINE_POOL, MachinePool
from shared.contracts import ContractModel
from shared.env import (
    GATEWAY_GRPC_HOST_ENV,
    GATEWAY_GRPC_PORT_ENV,
    GATEWAY_GRPC_TLS_ENV,
    GATEWAY_HTTP_HOST_ENV,
    GATEWAY_HTTP_PORT_ENV,
    GATEWAY_HTTP_TLS_ENV,
    GATEWAY_HTTP_URL_ENV,
    WORKER_REPOSITORY_URL_ENV,
)
from shared.gpu import normalize_gpu_type
from shared.routing import BackendRouteTransport
from shared.timestamps import utc_now
from shared.usage import UsageBillingOwner
from worker.configuration import (
    DEFAULT_WORKER_CONFIG_PATH,
    WORKER_CONFIG_PATH_ENV,
    WorkerCapacityConfiguration,
    WorkerConfiguration,
    WorkerExecutionConfiguration,
    WorkerMonitoringConfiguration,
    WorkerNetworkConfiguration,
    WorkerPathConfiguration,
)
from worker.events import WorkerPoolMode
from worker.execution import (
    DEFAULT_CONTAINER_BRIDGE_NAME,
    DEFAULT_CONTAINER_IPV6_SUBNET,
    DEFAULT_CONTAINER_SUBNET,
)
from worker.runtime_config import OciRuntimeName

from agent.binary import normalize_agent_artifact_config, normalize_agent_binary_name
from agent.service_manager import (
    DEFAULT_AGENT_STATE_DIR,
    AgentServiceRuntimeStatus,
    PreflightCheckName,
)

AGENT_SOURCE_CACHE_RELATIVE_PATH = Path("cache") / "source-code"

# Named because the agent both stamps these and searches by them: a worker
# container outlives its process so its logs can be read, and the label is the
# only way to find one whose slot the control plane has forgotten.
AGENT_MANAGED_LABEL = f"{NAME}.agent.managed"
AGENT_WORKER_ID_LABEL = f"{NAME}.agent.worker_id"
AGENT_WORKER_CONTAINER_SERVICE_BASE_PORT = 19000
AGENT_WORKER_CONTAINER_SERVICE_PORT_SPAN = 20000
AGENT_RUNTIME_READY_FILE = "runtime-ready.json"
AGENT_AUTHORITY_REVOKED_FILE = "authority-revoked.json"
AGENT_SERVICE_READY_TIMEOUT_SECONDS = 180
DOCKER_NETWORK_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$"


class AgentInstallOS(StrEnum):
    Linux = "linux"


class AgentInstallArch(StrEnum):
    Amd64 = "amd64"
    Arm64 = "arm64"


class AgentWorkerNetwork(ContractModel):
    name: str = Field(default="host", pattern=DOCKER_NETWORK_NAME_PATTERN)
    bridge_name: str = DEFAULT_CONTAINER_BRIDGE_NAME
    bridge_subnet: str = DEFAULT_CONTAINER_SUBNET
    bridge_ipv6_subnet: str = DEFAULT_CONTAINER_IPV6_SUBNET
    """The container bridge this agent's workers build on the host.

    A local fact about the machine rather than something the control plane assigns,
    which is why it is not carried on the bootstrap: two agents sharing a host must be
    told apart here, and nothing upstream knows they share one.
    """


class AgentJoinRequest(ContractModel):
    name: str
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    endpoint: str = "http://127.0.0.1:9000"
    token_secret: str | None = None
    version: str = "local"
    labels: dict[str, str] = Field(default_factory=dict)


class AgentStatusSummary(ContractModel):
    agents: int
    active_leases: int
    pools: dict[str, int] = Field(default_factory=dict)


class AgentHostStatus(ContractModel):
    joined: bool = False
    state_path: str
    active_worker_count: int = 0
    workspace_id: str = ""
    pool: MachinePool = MachinePool("")
    machine_id: str = ""
    gateway_url: str = ""
    service: AgentServiceRuntimeStatus


def build_join_command(request: AgentJoinRequest) -> list[str]:
    command = [
        ADMIN_CLI_NAME,
        "agent",
        "join",
        "--name",
        request.name,
        "--pool",
        str(request.pool),
        "--endpoint",
        request.endpoint,
        "--version",
        request.version,
    ]
    if request.token_secret:
        command.extend(["--token-secret", request.token_secret])
    for key, value in request.labels.items():
        command.extend(["--label", f"{key}={value}"])
    return command


def agent_binary_filename(
    os_name: AgentInstallOS,
    arch: AgentInstallArch,
    *,
    binary_name: str = AGENT_NAME,
) -> str:
    name = normalize_agent_binary_name(binary_name)
    return f"{name}-{os_name.value}-{arch.value}"


def build_agent_install_script(
    *,
    binary_name: str = AGENT_NAME,
    artifact_version: str = "",
    sha256_by_arch: Mapping[str, str] | None = None,
) -> str:
    name = normalize_agent_binary_name(binary_name)
    version, digests = normalize_agent_artifact_config(
        artifact_version,
        sha256_by_arch or {},
    )
    amd64_sha256 = digests.get("amd64", "")
    arm64_sha256 = digests.get("arm64", "")
    script = """#!/usr/bin/env sh
set -eu

GATEWAY=""
JOIN_TOKEN=""
PROVIDER_ENROLLMENT_REQUEST=""
PROVIDER=""
PROVIDER_INSTANCE_IDENTITY=""
MACHINE_FINGERPRINT=""
AGENT_HOSTNAME=""
DEV="0"
AGENT_BIN="${LAZYCLOUD_AGENT_BIN:-}"
AGENT_URL="${LAZYCLOUD_AGENT_URL:-}"
INSTALL_ONLY="0"
RUNTIME_ONLY="0"
AGENT_VERSION="${LAZYCLOUD_AGENT_VERSION:-}"
AGENT_SHA256="${LAZYCLOUD_AGENT_SHA256:-}"
AGENT_AMD64_SHA256="${LAZYCLOUD_AGENT_AMD64_SHA256:-}"
AGENT_ARM64_SHA256="${LAZYCLOUD_AGENT_ARM64_SHA256:-}"
CONFIGURED_AGENT_VERSION="__AGENT_VERSION__"
CONFIGURED_AGENT_AMD64_SHA256="__AGENT_AMD64_SHA256__"
CONFIGURED_AGENT_ARM64_SHA256="__AGENT_ARM64_SHA256__"
BACKGROUND="${LAZYCLOUD_AGENT_BACKGROUND:-auto}"
SERVICE_MANAGER="${LAZYCLOUD_AGENT_SERVICE_MANAGER:-auto}"
SERVICE_NAME="${LAZYCLOUD_AGENT_SERVICE_NAME:-__AGENT_NAME__}"
STATE_DIR="${LAZYCLOUD_AGENT_STATE_DIR:-}"
INSTALL_DOCKER="${LAZYCLOUD_AGENT_INSTALL_DOCKER:-auto}"
INSTALL_WIREGUARD="${LAZYCLOUD_AGENT_INSTALL_WIREGUARD:-auto}"
READY_TIMEOUT_SECONDS="${LAZYCLOUD_AGENT_READY_TIMEOUT_SECONDS:-__READY_TIMEOUT_SECONDS__}"
DOCKER_BINARY="${LAZYCLOUD_AGENT_DOCKER_BINARY:-docker}"
EXECUTOR=""
WORKER_IMAGE=""
WORKER_NETWORK="host"
MAX_CPU=""
MAX_MEMORY=""
MAX_GPUS=""
GPU_IDS=""
OS_RELEASE_FILE="${LAZYCLOUD_AGENT_OS_RELEASE_FILE:-/etc/os-release}"
OS_RELEASE_ID=""
OS_RELEASE_VERSION=""
OS_NAME=""
ARCH_NAME=""

main() {
  parse_args "$@"
  detect_platform
  if [ "$RUNTIME_ONLY" != "1" ]; then
    resolve_agent_artifact_digest
  fi
  validate_input
  GATEWAY="${GATEWAY%/}"

  if should_install_service; then
    require_systemd
  fi
  ensure_docker
  ensure_wireguard
  if [ "$RUNTIME_ONLY" = "1" ]; then
    say "Installed __AGENT_NAME__ host runtime"
    return
  fi
  install_agent
  if [ "$INSTALL_ONLY" = "1" ]; then
    say "Installed __AGENT_NAME__ runtime without enrolling"
    return
  fi
  run_agent
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --gateway) require_value "$1" "${2:-}"; GATEWAY="$2"; shift 2 ;;
      --join-token) require_value "$1" "${2:-}"; JOIN_TOKEN="$2"; shift 2 ;;
      --provider-enrollment-request)
        require_value "$1" "${2:-}"
        PROVIDER_ENROLLMENT_REQUEST="$2"
        shift 2
        ;;
      --provider) require_value "$1" "${2:-}"; PROVIDER="$2"; shift 2 ;;
      --provider-instance-identity)
        require_value "$1" "${2:-}"
        PROVIDER_INSTANCE_IDENTITY="$2"
        shift 2
        ;;
      --machine-fingerprint)
        require_value "$1" "${2:-}"
        MACHINE_FINGERPRINT="$2"
        shift 2
        ;;
      --hostname) require_value "$1" "${2:-}"; AGENT_HOSTNAME="$2"; shift 2 ;;
      --dev) DEV="1"; shift ;;
      --agent-bin) require_value "$1" "${2:-}"; AGENT_BIN="$2"; shift 2 ;;
      --agent-url) require_value "$1" "${2:-}"; AGENT_URL="$2"; shift 2 ;;
      --install-only) INSTALL_ONLY="1"; shift ;;
      --runtime-only) RUNTIME_ONLY="1"; shift ;;
      --agent-version) require_value "$1" "${2:-}"; AGENT_VERSION="$2"; shift 2 ;;
      --agent-sha256) require_value "$1" "${2:-}"; AGENT_SHA256="$2"; shift 2 ;;
      --agent-amd64-sha256)
        require_value "$1" "${2:-}"
        AGENT_AMD64_SHA256="$2"
        shift 2
        ;;
      --agent-arm64-sha256)
        require_value "$1" "${2:-}"
        AGENT_ARM64_SHA256="$2"
        shift 2
        ;;
      --background) BACKGROUND="1"; shift ;;
      --foreground) BACKGROUND="0"; shift ;;
      --service-manager) require_value "$1" "${2:-}"; SERVICE_MANAGER="$2"; shift 2 ;;
      --service-name) require_value "$1" "${2:-}"; SERVICE_NAME="$2"; shift 2 ;;
      --state-dir) require_value "$1" "${2:-}"; STATE_DIR="$2"; shift 2 ;;
      --install-docker) require_value "$1" "${2:-}"; INSTALL_DOCKER="$2"; shift 2 ;;
      --no-install-docker) INSTALL_DOCKER="never"; shift ;;
      --install-wireguard) require_value "$1" "${2:-}"; INSTALL_WIREGUARD="$2"; shift 2 ;;
      --no-install-wireguard) INSTALL_WIREGUARD="never"; shift ;;
      --docker-binary) require_value "$1" "${2:-}"; DOCKER_BINARY="$2"; shift 2 ;;
      --executor) require_value "$1" "${2:-}"; EXECUTOR="$2"; shift 2 ;;
      --worker-image) require_value "$1" "${2:-}"; WORKER_IMAGE="$2"; shift 2 ;;
      --worker-network) require_value "$1" "${2:-}"; WORKER_NETWORK="$2"; shift 2 ;;
      --max-cpu) require_value "$1" "${2:-}"; MAX_CPU="$2"; shift 2 ;;
      --max-memory) require_value "$1" "${2:-}"; MAX_MEMORY="$2"; shift 2 ;;
      --max-gpus) require_value "$1" "${2:-}"; MAX_GPUS="$2"; shift 2 ;;
      --gpu-ids) require_value "$1" "${2:-}"; GPU_IDS="$2"; shift 2 ;;
      *) fail "unknown argument: $1" 2 ;;
    esac
  done
}

detect_platform() {
  OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')"
  ARCH_NAME="$(uname -m)"
  OS_RELEASE_ID="$(read_os_release_value ID)"
  OS_RELEASE_VERSION="$(read_os_release_value VERSION_ID)"
  case "$ARCH_NAME" in
    x86_64|amd64) ARCH_NAME="amd64" ;;
    aarch64|arm64) ARCH_NAME="arm64" ;;
  esac
}

read_os_release_value() {
  key="$1"
  if [ ! -r "$OS_RELEASE_FILE" ]; then
    return
  fi
  value="$(sed -n "s/^${key}=//p" "$OS_RELEASE_FILE" | sed -n '1p' | tr -d '\\042\\047')"
  printf '%s\n' "$value"
}

resolve_agent_artifact_digest() {
  if [ "$DEV" != "1" ] && [ -z "$AGENT_VERSION" ] && [ -z "$AGENT_SHA256" ] && \
      [ -z "$AGENT_AMD64_SHA256" ] && [ -z "$AGENT_ARM64_SHA256" ]; then
    AGENT_VERSION="$CONFIGURED_AGENT_VERSION"
    AGENT_AMD64_SHA256="$CONFIGURED_AGENT_AMD64_SHA256"
    AGENT_ARM64_SHA256="$CONFIGURED_AGENT_ARM64_SHA256"
  fi
  if [ -n "$AGENT_SHA256" ]; then
    if [ -n "$AGENT_AMD64_SHA256" ] || [ -n "$AGENT_ARM64_SHA256" ]; then
      fail "--agent-sha256 cannot be combined with architecture-specific digests" 2
    fi
    return
  fi
  case "$ARCH_NAME" in
    amd64) AGENT_SHA256="$AGENT_AMD64_SHA256" ;;
    arm64) AGENT_SHA256="$AGENT_ARM64_SHA256" ;;
  esac
}

validate_input() {
  if [ "$RUNTIME_ONLY" = "1" ]; then
    if [ "$INSTALL_ONLY" = "1" ] || [ -n "$JOIN_TOKEN" ] || \
        [ -n "$PROVIDER_ENROLLMENT_REQUEST" ] || [ -n "$AGENT_URL" ] || \
        [ -n "$AGENT_BIN" ]; then
      fail "--runtime-only cannot install or enroll an agent" 2
    fi
  # An install-only run installs the runtime and agent but never enrolls. There
  # is no control plane to name and no credential to carry.
  elif [ "$INSTALL_ONLY" = "1" ]; then
    if [ -n "$JOIN_TOKEN" ] || [ -n "$PROVIDER_ENROLLMENT_REQUEST" ]; then
      fail "--install-only cannot be combined with an enrollment credential" 2
    fi
    if [ -z "$AGENT_URL" ]; then
      fail "--install-only requires --agent-url" 2
    fi
  else
    if [ -z "$GATEWAY" ]; then
      fail "--gateway is required" 2
    fi
    if [ -n "$JOIN_TOKEN" ] && [ -n "$PROVIDER_ENROLLMENT_REQUEST" ]; then
      fail "--join-token and --provider-enrollment-request cannot be combined" 2
    fi
    if [ -z "$JOIN_TOKEN" ] && [ -z "$PROVIDER_ENROLLMENT_REQUEST" ]; then
      fail "--join-token or --provider-enrollment-request is required" 2
    fi
  fi
  if [ -n "$PROVIDER_ENROLLMENT_REQUEST" ]; then
    if [ -z "$PROVIDER" ] || [ -z "$PROVIDER_INSTANCE_IDENTITY" ]; then
      fail "provider enrollment requires --provider and --provider-instance-identity" 2
    fi
  fi
  if [ -n "$GATEWAY" ]; then
    case "$GATEWAY" in
      http://*|https://*) ;;
      *) fail "--gateway must start with http:// or https://" 2 ;;
    esac
  fi
  if [ -n "$AGENT_URL" ]; then
    # The artifact is public and unauthenticated, so it must not be reached
    # over a scheme that would carry the enrolment credential in the clear.
    case "$AGENT_URL" in
      https://*) ;;
      *) fail "--agent-url must start with https://" 2 ;;
    esac
    case "$AGENT_URL" in
      *@*) fail "--agent-url must not embed credentials" 2 ;;
    esac
  fi
  if [ "$OS_NAME" != "linux" ]; then
    fail "unsupported operating system: $OS_NAME; customer machines require Linux" 1
  fi
  if [ "$ARCH_NAME" != "amd64" ] && [ "$ARCH_NAME" != "arm64" ]; then
    fail "unsupported architecture: $ARCH_NAME; expected amd64 or arm64" 1
  fi
  # A digest is only meaningful next to the artifact it describes. A version
  # names one by deriving its URL; --agent-url names one outright, so it
  # satisfies the same requirement without a version to derive anything from.
  if [ -n "$AGENT_VERSION" ] || [ -n "$AGENT_SHA256" ]; then
    if [ -z "$AGENT_SHA256" ] || { [ -z "$AGENT_VERSION" ] && [ -z "$AGENT_URL" ]; }; then
      fail "--agent-sha256 needs the artifact it describes: pass --agent-version or --agent-url" 2
    fi
    # Only checked when a version is what names the artifact; --agent-url
    # carries no version and needs none.
    case "$AGENT_VERSION" in
      '') [ -n "$AGENT_URL" ] || fail "--agent-version is required" 2 ;;
      *[!A-Za-z0-9._-]*) fail "--agent-version contains invalid characters" 2 ;;
    esac
    case "$AGENT_SHA256" in
      *[!0-9a-f]*|'') fail "--agent-sha256 must be a lowercase SHA-256 digest" 2 ;;
    esac
    if [ "${#AGENT_SHA256}" -ne 64 ]; then
      fail "--agent-sha256 must be a lowercase SHA-256 digest" 2
    fi
  fi
  case "$SERVICE_MANAGER" in
    auto|systemd) ;;
    *) fail "unsupported service manager: $SERVICE_MANAGER; expected auto or systemd" 2 ;;
  esac
  case "$INSTALL_DOCKER" in
    auto|1|true|yes|0|false|no|never) ;;
    *) fail "--install-docker must be auto, true, or never" 2 ;;
  esac
  case "$INSTALL_WIREGUARD" in
    auto|1|true|yes|0|false|no|never) ;;
    *) fail "--install-wireguard must be auto, true, or never" 2 ;;
  esac
  case "$READY_TIMEOUT_SECONDS" in
    ""|*[!0-9]*) fail "LAZYCLOUD_AGENT_READY_TIMEOUT_SECONDS must be a positive integer" 2 ;;
  esac
  if [ "$READY_TIMEOUT_SECONDS" -le 0 ]; then
    fail "LAZYCLOUD_AGENT_READY_TIMEOUT_SECONDS must be a positive integer" 2
  fi
  case "$EXECUTOR" in
    ""|container|external) ;;
    *) fail "--executor must be container or external" 2 ;;
  esac
}

should_install_service() {
  if [ "$BACKGROUND" = "1" ]; then
    return 0
  fi
  if [ "$BACKGROUND" = "0" ] || [ "$DEV" = "1" ]; then
    return 1
  fi
  return 0
}

require_systemd() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "background installation requires root; rerun with sudo or use --foreground" 1
  fi
  if ! command -v systemctl >/dev/null 2>&1 || \
      ! systemctl show-environment >/dev/null 2>&1; then
    fail "systemd is required for background installation; use --foreground on this host" 1
  fi
}

needs_container_executor() {
  [ -z "$EXECUTOR" ] || [ "$EXECUTOR" = "container" ]
}

docker_ready() {
  command -v "$DOCKER_BINARY" >/dev/null 2>&1 && "$DOCKER_BINARY" info >/dev/null 2>&1
}

docker_install_allowed() {
  case "$INSTALL_DOCKER" in
    auto|1|true|yes) return 0 ;;
    *) return 1 ;;
  esac
}

start_docker() {
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl enable --now docker >/dev/null 2>&1
  elif command -v service >/dev/null 2>&1; then
    service docker start >/dev/null 2>&1
  else
    return 1
  fi
}

wait_for_docker() {
  attempt=0
  while [ "$attempt" -lt 20 ]; do
    if docker_ready; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  return 1
}

ensure_docker() {
  if ! needs_container_executor; then
    return
  fi
  if docker_ready; then
    return
  fi
  if command -v "$DOCKER_BINARY" >/dev/null 2>&1; then
    say "Starting Docker"
    if ! start_docker; then
      fail "unable to start Docker service" 1
    fi
    if wait_for_docker; then
      return
    fi
    fail "Docker daemon unavailable; start Docker and verify '$DOCKER_BINARY info'" 1
  fi
  if ! docker_install_allowed; then
    fail "Docker is required for container execution; install Docker or use --install-docker auto" 1
  fi
  if [ "$DOCKER_BINARY" != "docker" ]; then
    fail "custom Docker binary '$DOCKER_BINARY' was not found; install it manually" 1
  fi
  if [ "$(id -u)" -ne 0 ]; then
    fail "automatic Docker installation requires root; rerun with sudo or install Docker first" 1
  fi

  say "Installing Docker"
  install_docker
  if ! start_docker; then
    fail "Docker was installed but its service could not be enabled and started" 1
  fi
  if ! wait_for_docker; then
    fail "Docker was installed but did not become ready; start Docker and rerun this command" 1
  fi
}

install_docker() {
  if [ "$OS_RELEASE_ID" = "amzn" ]; then
    case "$OS_RELEASE_VERSION" in
      2023|2023.*)
        if ! command -v dnf >/dev/null 2>&1; then
          fail "Amazon Linux 2023 requires dnf to install Docker" 1
        fi
        if ! dnf install -y docker; then
          fail "Docker package installation failed on Amazon Linux 2023" 1
        fi
        return
        ;;
      *)
        fail "automatic Docker installation supports Amazon Linux 2023 only on Amazon Linux hosts" 1
        ;;
    esac
  fi

  docker_installer="$(mktemp)"
  if ! download_file "https://get.docker.com" "$docker_installer"; then
    rm -f "$docker_installer"
    fail "unable to download the official Docker installer" 1
  fi
  if ! sh "$docker_installer"; then
    rm -f "$docker_installer"
    fail "Docker installation failed" 1
  fi
  rm -f "$docker_installer"
}

wireguard_ready() {
  command -v wg >/dev/null 2>&1 && \
    command -v ip >/dev/null 2>&1 && \
    command -v iptables >/dev/null 2>&1
}

wireguard_install_allowed() {
  case "$INSTALL_WIREGUARD" in
    auto|1|true|yes) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_wireguard() {
  if wireguard_ready; then
    return
  fi
  if ! wireguard_install_allowed; then
    fail "WireGuard tools are required; use --install-wireguard auto" 1
  fi
  if [ "$(id -u)" -ne 0 ]; then
    fail "automatic WireGuard installation requires root; rerun with sudo" 1
  fi

  say "Installing WireGuard tools"
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y wireguard-tools iproute iptables || \
      fail "WireGuard package installation failed" 1
  elif command -v apt-get >/dev/null 2>&1; then
    apt-get update && apt-get install -y --no-install-recommends \
      wireguard-tools iproute2 iptables || \
      fail "WireGuard package installation failed" 1
  else
    fail "automatic WireGuard installation requires dnf or apt-get" 1
  fi
  if ! wireguard_ready; then
    fail "WireGuard tools were installed but are not available" 1
  fi
}

install_agent() {
  if [ -n "$AGENT_BIN" ]; then
    if [ ! -x "$AGENT_BIN" ]; then
      fail "agent binary is not executable: $AGENT_BIN" 1
    fi
    return
  fi
  if [ "$(id -u)" -eq 0 ]; then
    AGENT_BIN="/usr/local/bin/__AGENT_NAME__"
  else
    AGENT_BIN="${HOME:-/tmp}/__HOME_DIR__/bin/__AGENT_NAME__"
  fi
  # A published artifact URL wins: a managed node pulls 47 MB per launch, and
  # serving that from the control plane makes every scale-up its problem.
  if [ -n "$AGENT_URL" ]; then
    install_from_url "$AGENT_URL" "$AGENT_BIN"
    return
  fi
  artifact_url="$GATEWAY/install/agent/$OS_NAME/$ARCH_NAME"
  if [ -n "$AGENT_VERSION" ]; then
    artifact_url="$GATEWAY/install/agent/$AGENT_VERSION/$OS_NAME/$ARCH_NAME"
  fi
  install_from_url "$artifact_url" "$AGENT_BIN"
}

install_from_url() {
  url="$1"
  destination="$2"
  destination_dir="$(dirname "$destination")"
  mkdir -p "$destination_dir"
  temporary="$(mktemp "$destination_dir/.agent.XXXXXX")"
  say "Installing __AGENT_NAME__"
  if ! download_file "$url" "$temporary"; then
    rm -f "$temporary"
    fail "unable to download the agent binary from $url" 1
  fi
  if [ -n "$AGENT_SHA256" ]; then
    verify_sha256 "$temporary" "$AGENT_SHA256" "agent artifact"
  fi
  chmod 0755 "$temporary"
  if [ -d "$destination" ]; then
    rm -f "$temporary"
    fail "$destination is a directory; remove it before installing" 1
  fi
  mv -f "$temporary" "$destination"
}

verify_sha256() {
  file="$1"
  expected="$2"
  artifact="${3:-artifact}"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$file" | awk '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  else
    rm -f "$file"
    fail "sha256sum or shasum is required to verify the agent artifact" 1
  fi
  if [ "$actual" != "$expected" ]; then
    rm -f "$file"
    fail "$artifact SHA-256 mismatch" 1
  fi
}

download_file() {
  download_url="$1"
  download_destination="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$download_url" -o "$download_destination"
    return
  fi
  if command -v wget >/dev/null 2>&1; then
    wget -q -O "$download_destination" "$download_url"
    return
  fi
  fail "curl or wget is required to download installation artifacts" 1
}

run_agent() {
  set -- join --gateway "$GATEWAY"
  [ -n "$JOIN_TOKEN" ] && set -- "$@" --join-token "$JOIN_TOKEN"
  if [ -n "$PROVIDER_ENROLLMENT_REQUEST" ]; then
    set -- "$@" \
      --provider-enrollment-request "$PROVIDER_ENROLLMENT_REQUEST" \
      --provider "$PROVIDER" \
      --provider-instance-identity "$PROVIDER_INSTANCE_IDENTITY"
  fi
  [ -n "$MACHINE_FINGERPRINT" ] && set -- "$@" --machine-fingerprint "$MACHINE_FINGERPRINT"
  [ -n "$AGENT_HOSTNAME" ] && set -- "$@" --hostname "$AGENT_HOSTNAME"
  [ -n "$EXECUTOR" ] && set -- "$@" --executor "$EXECUTOR"
  [ -n "$WORKER_IMAGE" ] && set -- "$@" --worker-image "$WORKER_IMAGE"
  [ "$WORKER_NETWORK" != "host" ] && set -- "$@" --worker-network "$WORKER_NETWORK"
  [ -n "$MAX_CPU" ] && set -- "$@" --max-cpu "$MAX_CPU"
  [ -n "$MAX_MEMORY" ] && set -- "$@" --max-memory "$MAX_MEMORY"
  [ -n "$MAX_GPUS" ] && set -- "$@" --max-gpus "$MAX_GPUS"
  [ -n "$GPU_IDS" ] && set -- "$@" --gpu-ids "$GPU_IDS"
  [ "$DOCKER_BINARY" != "docker" ] && set -- "$@" --docker-binary "$DOCKER_BINARY"
  [ -n "$STATE_DIR" ] && set -- "$@" --state-dir "$STATE_DIR"

  if should_install_service; then
    say "Installing __AGENT_NAME__ service"
    shift
    set -- install-service --target systemd --service-name "$SERVICE_NAME" "$@"
    "$AGENT_BIN" "$@"
    wait_for_agent_service
    return
  else
    say "Starting __AGENT_NAME__"
  fi
  exec "$AGENT_BIN" "$@"
}

wait_for_agent_service() {
  state_dir="$STATE_DIR"
  [ -n "$state_dir" ] || state_dir="__DEFAULT_AGENT_STATE_DIR__"
  ready_path="$state_dir/__AGENT_RUNTIME_READY_FILE__"
  unit_name="$SERVICE_NAME.service"
  elapsed=0

  say "Waiting for __AGENT_NAME__ enrollment"
  while [ "$elapsed" -lt "$READY_TIMEOUT_SECONDS" ]; do
    if [ -s "$ready_path" ] && systemctl is-active --quiet "$unit_name"; then
      say "__AGENT_NAME__ enrolled and ready"
      return
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done

  say "__AGENT_NAME__ did not enroll within ${READY_TIMEOUT_SECONDS}s"
  report_agent_service_diagnostics "$unit_name"
  fail "__AGENT_NAME__ service failed its enrollment readiness check" 1
}

report_agent_service_diagnostics() {
  unit_name="$1"
  say "Service state"
  systemctl show "$unit_name" \
    --no-pager \
    --property=ActiveState,SubState,Result,NRestarts \
    2>&1 | redact_agent_diagnostics >&2 || true
  if command -v journalctl >/dev/null 2>&1; then
    say "Recent service diagnostics"
    journalctl \
      --unit "$unit_name" \
      --no-pager \
      --output cat \
      --lines 80 \
      2>&1 | redact_agent_diagnostics >&2 || true
  fi
}

redact_agent_diagnostics() {
  sed -E \
    -e 's#(https?://[^?[:space:]]+)\\?[^[:space:]]+#\\1?[REDACTED]#g' \
    -e 's#(Authorization:[[:space:]]*(Bearer|Basic)[[:space:]]+)[^[:space:]]+#\\1[REDACTED]#g' \
    -e 's#(AWS_SECRET_ACCESS_KEY[=:][[:space:]]*)[^[:space:]]+#\\1[REDACTED]#g' \
    -e 's#(SECRET_ACCESS_KEY[=:][[:space:]]*)[^[:space:]]+#\\1[REDACTED]#g' \
    -e 's#((AWS_SESSION_TOKEN|SESSION_TOKEN)[=:][[:space:]]*)[^[:space:]]+#\\1[REDACTED]#g' \
    -e 's#((token|Token|TOKEN)[=:][[:space:]]*)[^[:space:]]+#\\1[REDACTED]#g' \
    -e 's#((password|Password|PASSWORD)[=:][[:space:]]*)[^[:space:]]+#\\1[REDACTED]#g' \
    -e 's#((secret|Secret|SECRET)[=:][[:space:]]*)[^[:space:]]+#\\1[REDACTED]#g'
}

require_value() {
  if [ -z "$2" ]; then
    fail "$1 requires a value" 2
  fi
}

say() {
  printf '=> %s\n' "$1" >&2
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit "$2"
}

main "$@"
"""
    return (
        script.replace("__AGENT_NAME__", name)
        .replace("__HOME_DIR__", HOME_DIR)
        .replace("__DEFAULT_AGENT_STATE_DIR__", DEFAULT_AGENT_STATE_DIR)
        .replace("__AGENT_RUNTIME_READY_FILE__", AGENT_RUNTIME_READY_FILE)
        .replace("__READY_TIMEOUT_SECONDS__", str(AGENT_SERVICE_READY_TIMEOUT_SECONDS))
        .replace("__AGENT_VERSION__", version)
        .replace("__AGENT_AMD64_SHA256__", amd64_sha256)
        .replace("__AGENT_ARM64_SHA256__", arm64_sha256)
    )


def redact_telemetry(text: str) -> str:
    return redact_telemetry_line(text)


def summarize_agent_status(agent_pools: list[str], active_leases: int) -> AgentStatusSummary:
    pools: dict[str, int] = {}
    for pool in agent_pools:
        pools[pool] = pools.get(pool, 0) + 1
    return AgentStatusSummary(agents=len(agent_pools), active_leases=active_leases, pools=pools)


class AgentCapacityCheckName(StrEnum):
    MaxCpu = "capacity.max_cpu"
    MaxMemory = "capacity.max_memory"
    GpuSelection = "capacity.gpu_selection"
    GpuIds = "capacity.gpu_ids"
    MaxGpus = "capacity.max_gpus"
    NvidiaRuntime = "nvidia-runtime"


class WorkerExecutor(StrEnum):
    Container = "container"
    External = "external"


class WorkerSlotAction(StrEnum):
    Keep = "keep"
    Prepare = "prepare"
    Start = "start"
    Restart = "restart"
    Stop = "stop"
    StopAll = "stop-all"
    Unsupported = "unsupported"


class AgentCapacityCheck(ContractModel):
    name: AgentCapacityCheckName | PreflightCheckName
    ok: bool
    message: str
    severity: PreflightSeverity = PreflightSeverity.Info


class AgentGpuDevice(ContractModel):
    id: str
    uuid: str = ""
    name: str


class AgentDetectedResources(ContractModel):
    cpu_count: int = 1
    memory_mb: int = 1024
    gpus: list[AgentGpuDevice] = Field(default_factory=list)

    @field_validator("cpu_count", "memory_mb")
    @classmethod
    def detected_values_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            msg = "detected resource values must be positive"
            raise ValueError(msg)
        return value


class AgentResourceDetection(ContractModel):
    resources: AgentDetectedResources
    checks: list[AgentCapacityCheck] = Field(default_factory=list)
    schedulable: bool = True


class AgentCapacityOptions(ContractModel):
    max_cpu: str = ""
    max_memory: str = ""
    max_gpus: int = 0
    gpu_ids: str = ""

    @field_validator("max_gpus")
    @classmethod
    def option_counts_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "agent capacity options cannot be negative"
            raise ValueError(msg)
        return value


class AgentCapacity(ContractModel):
    cpu_count: int
    cpu_millicores: int
    memory_mb: int
    gpus: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    gpu_count: int = 0


class AgentCapacityPlan(ContractModel):
    capacity: AgentCapacity
    checks: list[AgentCapacityCheck] = Field(default_factory=list)
    schedulable: bool = True


class AgentBootstrap(ContractModel):
    gateway_public_http_url: str
    gateway_runtime_http_url: str = ""
    gateway_grpc_host: str = ""
    gateway_grpc_port: int = 443
    gateway_grpc_tls: bool = True
    transport: BackendRouteTransport = BackendRouteTransport.Direct
    image_local_cache_enabled: bool = True
    image_registry_store: str = "local"
    image_clip_version: int = 2

    @field_validator("gateway_grpc_port", "image_clip_version")
    @classmethod
    def positive_bootstrap_ints(cls, value: int) -> int:
        if value <= 0:
            msg = "bootstrap numeric values must be positive"
            raise ValueError(msg)
        return value


class AgentState(ContractModel):
    gateway_url: str
    workspace_id: str
    pool: MachinePool
    machine_id: str
    agent_token: str
    credential_id: str
    credential_generation: int = Field(ge=1)
    capacity_state: AgentCapacityState = AgentCapacityState.Available
    capacity_reason: str = ""
    capacity_observed_at: datetime | None = None
    capacity_notice_at: datetime | None = None
    bootstrap: AgentBootstrap
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def sanitized_gateway_url(self) -> str:
        return normalize_gateway_url(self.gateway_url)


class AgentRuntimeReady(ContractModel):
    machine_id: str
    stream_iteration: int = Field(ge=1)
    ready_at: datetime = Field(default_factory=utc_now)


class AgentAuthorityRevoked(ContractModel):
    """Written once when the control plane revokes this machine's authority.

    Its presence is what stops the next start from re-joining. Revocation is the
    one stream ending that a restart cannot recover from, so it is recorded on
    disk rather than inferred from the absence of saved state.
    """

    machine_id: str
    revoked_at: datetime = Field(default_factory=utc_now)


class AgentCapacityInterruptionNotice(ContractModel):
    reason: str = Field(min_length=1, max_length=240)
    observed_at: datetime = Field(default_factory=utc_now)
    notice_at: datetime | None = None


class AgentLockPlan(ContractModel):
    state_dir: str
    path: str
    contents: str
    permissions: int = 0o600


class AgentWorkerDirs(ContractModel):
    slot: str
    images: str
    tmp: str
    data: str
    workspace: str
    cache: str
    builds: str
    checkpoints: str
    logs: str

    def all_paths(self) -> list[str]:
        return [
            self.slot,
            self.images,
            self.tmp,
            self.data,
            self.workspace,
            self.cache,
            self.builds,
            self.checkpoints,
            self.logs,
        ]


class AgentWorkerSlot(ContractModel):
    worker_id: str
    worker_token: str = ""
    pool: MachinePool = MachinePool(LAZYCLOUD_MACHINE_POOL)
    capacity_owner_id: str = Field(pattern=CAPACITY_OWNER_ID_PATTERN)
    billing_owner: UsageBillingOwner
    machine_id: str = ""
    cpu_millicores: int = 0
    memory_mb: int = 0
    gpu: str = ""
    gpu_count: int = 0
    gpu_assignment: str = ""
    network_prefix: str = ""
    worker_image: str = ""
    status: AgentWorkerSlotStatus = AgentWorkerSlotStatus.Active

    @field_validator(
        "cpu_millicores",
        "memory_mb",
        "gpu_count",
    )
    @classmethod
    def slot_counts_cannot_be_negative(cls, value: int) -> int:
        if value < 0:
            msg = "worker slot resource values cannot be negative"
            raise ValueError(msg)
        return value


class AgentWorkerContainerPlan(ContractModel):
    slot: AgentWorkerSlot
    name: str
    image: str
    network: AgentWorkerNetwork
    config_path: str
    dirs: AgentWorkerDirs
    labels: dict[str, str]
    env: dict[str, str]
    volumes: list[str]
    docker_args: list[str]
    config: WorkerConfiguration


class AgentWorkerReconcileAction(ContractModel):
    action: WorkerSlotAction
    worker_id: str
    reason: str
    slot: AgentWorkerSlot | None = None


class AgentWorkerReconcilePlan(ContractModel):
    executor: WorkerExecutor
    actions: list[AgentWorkerReconcileAction] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(action.action != WorkerSlotAction.Keep for action in self.actions)


def parse_cpu_millicores(value: str) -> int:
    stripped = value.strip()
    if not stripped:
        msg = "max cpu is empty"
        raise ValueError(msg)
    try:
        cores = float(stripped)
    except ValueError as exc:
        msg = "max cpu must be a positive number of cores"
        raise ValueError(msg) from exc
    if cores <= 0:
        msg = "max cpu must be a positive number of cores"
        raise ValueError(msg)
    return math.floor(cores * 1000 + 0.5)


def parse_memory_mb(value: str) -> int:
    stripped = value.strip().lower()
    if not stripped:
        msg = "max memory is empty"
        raise ValueError(msg)
    units = [
        ("tib", 1024 * 1024),
        ("tb", 1000 * 1000),
        ("gib", 1024),
        ("gb", 1000),
        ("gi", 1024),
        ("g", 1000),
        ("mib", 1),
        ("mb", 1),
        ("mi", 1),
        ("m", 1),
    ]
    scale = 1
    for suffix, unit_scale in units:
        if stripped.endswith(suffix):
            stripped = stripped[: -len(suffix)].strip()
            scale = unit_scale
            break
    try:
        amount = float(stripped)
    except ValueError as exc:
        msg = "max memory must be a positive size"
        raise ValueError(msg) from exc
    if amount <= 0:
        msg = "max memory must be a positive size"
        raise ValueError(msg)
    return math.floor(amount * scale + 0.5)


def resolve_agent_capacity(
    options: AgentCapacityOptions,
    detected: AgentDetectedResources,
    *,
    base_checks: list[AgentCapacityCheck] | None = None,
    initially_schedulable: bool = True,
) -> AgentCapacityPlan:
    checks = list(base_checks or [])
    schedulable = initially_schedulable
    capacity = AgentCapacity(
        cpu_count=detected.cpu_count,
        cpu_millicores=detected.cpu_count * 1000,
        memory_mb=detected.memory_mb,
        gpus=[normalize_gpu_type(item.name) for item in detected.gpus],
        gpu_ids=[item.id for item in detected.gpus],
        gpu_count=len(detected.gpus),
    )

    if options.max_cpu:
        try:
            requested = parse_cpu_millicores(options.max_cpu)
            ok = requested <= detected.cpu_count * 1000
            message = f"using {requested}m CPU" if ok else "requested CPU exceeds detected CPU"
        except ValueError as exc:
            requested = 0
            ok = False
            message = str(exc)
        checks.append(_capacity_check(AgentCapacityCheckName.MaxCpu, ok, message))
        if ok:
            capacity.cpu_millicores = requested
            capacity.cpu_count = math.ceil(requested / 1000)
        else:
            schedulable = False

    if options.max_memory:
        try:
            requested_memory = parse_memory_mb(options.max_memory)
            ok = requested_memory <= detected.memory_mb
            message = (
                f"using {requested_memory} MB memory"
                if ok
                else "requested memory exceeds detected memory"
            )
        except ValueError as exc:
            requested_memory = 0
            ok = False
            message = str(exc)
        checks.append(_capacity_check(AgentCapacityCheckName.MaxMemory, ok, message))
        if ok:
            capacity.memory_mb = requested_memory
        else:
            schedulable = False

    selected, explicit, gpu_checks = select_agent_gpu_devices(options, detected.gpus)
    checks.extend(gpu_checks)
    if any(not check.ok for check in gpu_checks):
        schedulable = False
    if explicit:
        capacity.gpus = [normalize_gpu_type(item.name) for item in selected]
        capacity.gpu_ids = [item.id for item in selected]
        capacity.gpu_count = len(selected)

    return AgentCapacityPlan(capacity=capacity, checks=checks, schedulable=schedulable)


def select_agent_gpu_devices(
    options: AgentCapacityOptions,
    devices: list[AgentGpuDevice],
) -> tuple[list[AgentGpuDevice], bool, list[AgentCapacityCheck]]:
    gpu_ids = split_csv(options.gpu_ids)
    if gpu_ids and options.max_gpus > 0:
        return (
            [],
            True,
            [
                _capacity_check(
                    AgentCapacityCheckName.GpuSelection,
                    False,
                    "--gpu-ids and --max-gpus cannot both be set",
                )
            ],
        )
    if gpu_ids:
        selected: list[AgentGpuDevice] = []
        for gpu_id in gpu_ids:
            device = find_agent_gpu_device(devices, gpu_id)
            if device is None:
                return (
                    [],
                    True,
                    [
                        _capacity_check(
                            AgentCapacityCheckName.GpuIds,
                            False,
                            f"GPU id {gpu_id!r} was not detected",
                        )
                    ],
                )
            selected.append(device.model_copy(update={"id": gpu_id}))
        return (
            selected,
            True,
            [
                _capacity_check(
                    AgentCapacityCheckName.GpuIds,
                    True,
                    f"using GPU ids {','.join(gpu_ids)}",
                )
            ],
        )
    if options.max_gpus > 0:
        if options.max_gpus > len(devices):
            return (
                [],
                True,
                [
                    _capacity_check(
                        AgentCapacityCheckName.MaxGpus,
                        False,
                        f"requested {options.max_gpus} GPUs, detected {len(devices)}",
                    )
                ],
            )
        return (
            devices[: options.max_gpus],
            True,
            [
                _capacity_check(
                    AgentCapacityCheckName.MaxGpus,
                    True,
                    f"using {options.max_gpus} GPUs",
                )
            ],
        )
    return ([], False, [])


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_nvidia_smi_gpu_devices(output: str) -> list[AgentGpuDevice]:
    devices: list[AgentGpuDevice] = []
    for line in output.splitlines():
        fields = [field.strip() for field in line.split(",", 2)]
        if len(fields) != 3:
            continue
        gpu_id, uuid, name = fields
        normalized_name = normalize_gpu_type(name)
        if gpu_id and normalized_name:
            devices.append(AgentGpuDevice(id=gpu_id, uuid=uuid, name=normalized_name))
    return devices


def find_agent_gpu_device(
    devices: list[AgentGpuDevice],
    gpu_id: str,
) -> AgentGpuDevice | None:
    for device in devices:
        if device.id == gpu_id or device.uuid == gpu_id:
            return device
    return None


def normalize_gateway_url(value: str) -> str:
    return value.rstrip("/")


def agent_state_matches_gateway(state: AgentState, gateway_url: str) -> bool:
    return bool(state.agent_token) and state.sanitized_gateway_url == normalize_gateway_url(
        gateway_url
    )


def agent_state_payload(
    state: AgentState,
) -> dict[str, JsonValue]:
    bootstrap = state.bootstrap
    return {
        "gateway_url": state.gateway_url,
        "workspace_id": state.workspace_id,
        "pool": state.pool,
        "machine_id": state.machine_id,
        "agent_token": state.agent_token,
        "credential_id": state.credential_id,
        "credential_generation": state.credential_generation,
        "capacity_state": state.capacity_state.value,
        "capacity_reason": state.capacity_reason,
        "capacity_observed_at": (
            state.capacity_observed_at.isoformat()
            if state.capacity_observed_at is not None
            else None
        ),
        "capacity_notice_at": (
            state.capacity_notice_at.isoformat() if state.capacity_notice_at is not None else None
        ),
        # Dumped whole rather than field by field. The hand-written list omitted
        # `gateway_runtime_http_url`, so the origin survived in memory and was
        # lost on the next restart: the worker then fell back to the public
        # origin, which refuses worker RPC at the edge, and never left pending.
        "bootstrap": bootstrap.model_dump(mode="json"),
        "updated_at": state.updated_at.isoformat(),
    }


def plan_agent_lock(state_dir: str, *, pid: int) -> AgentLockPlan:
    if pid <= 0:
        msg = "pid must be positive"
        raise ValueError(msg)
    return AgentLockPlan(
        state_dir=state_dir,
        path=posixpath.join(state_dir.rstrip("/"), "agent.lock"),
        contents=f"pid={pid}\n",
    )


def sanitize_worker_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-_.")
    if not sanitized:
        sanitized = "slot"
    return sanitized[:96]


def build_agent_worker_dirs(state_dir: str, worker_id: str) -> AgentWorkerDirs:
    root = state_dir.rstrip("/")
    slot_name = sanitize_worker_name(worker_id)
    return AgentWorkerDirs(
        slot=posixpath.join(root, "slots", slot_name),
        images=posixpath.join(root, "images"),
        tmp=posixpath.join(root, "tmp", slot_name),
        data=posixpath.join(root, "data"),
        workspace=posixpath.join(root, "workspace-data"),
        cache=posixpath.join(root, "cache"),
        builds=posixpath.join(root, "builds"),
        checkpoints=posixpath.join(root, "checkpoints"),
        logs=posixpath.join(root, "logs", slot_name),
    )


def agent_gateway_env(bootstrap: AgentBootstrap) -> dict[str, str]:
    runtime_http_url = bootstrap.gateway_runtime_http_url or bootstrap.gateway_public_http_url
    http_host, http_port, http_tls = agent_gateway_http_parts(runtime_http_url)
    grpc_port = bootstrap.gateway_grpc_port or 443
    return {
        GATEWAY_GRPC_HOST_ENV: bootstrap.gateway_grpc_host,
        GATEWAY_GRPC_PORT_ENV: str(grpc_port),
        GATEWAY_GRPC_TLS_ENV: str(bootstrap.gateway_grpc_tls).lower(),
        GATEWAY_HTTP_HOST_ENV: http_host,
        GATEWAY_HTTP_PORT_ENV: str(http_port),
        GATEWAY_HTTP_TLS_ENV: str(http_tls).lower(),
        GATEWAY_HTTP_URL_ENV: normalize_gateway_url(runtime_http_url),
    }


def agent_gateway_http_parts(gateway_public_http_url: str) -> tuple[str, int, bool]:
    parsed = urlparse(gateway_public_http_url)
    if not parsed.hostname:
        return (gateway_public_http_url, 443, True)
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == "http" else 443
    return (parsed.hostname, port, parsed.scheme != "http")


def build_agent_worker_config(
    bootstrap: AgentBootstrap,
    slot: AgentWorkerSlot,
    *,
    network: AgentWorkerNetwork | None = None,
) -> WorkerConfiguration:
    selected_network = network or AgentWorkerNetwork()
    return WorkerConfiguration(
        execution=WorkerExecutionConfiguration(
            runtime=OciRuntimeName.Runsc,
            capacity=WorkerCapacityConfiguration(
                cpu_millicores=slot.cpu_millicores,
                memory_mib=slot.memory_mb,
                gpu_type=slot.gpu,
                gpu_count=slot.gpu_count,
            ),
            pool_mode=WorkerPoolMode.Private,
            billing_owner=slot.billing_owner,
            persistent=True,
            agent_worker=True,
        ),
        network=WorkerNetworkConfiguration(
            route_transport=bootstrap.transport,
            agent_bridge_network=bool(slot.network_prefix),
            bridge_name=selected_network.bridge_name,
            bridge_subnet=selected_network.bridge_subnet,
            bridge_ipv6_subnet=selected_network.bridge_ipv6_subnet,
        ),
        paths=WorkerPathConfiguration(
            bundle_root=Path(AGENT_CONTAINER_TMP_PATH) / "bundles",
            image_cache_path="/images/cache",
            image_mount_root="/images/mounts",
            image_build_root=Path("/builds"),
            cache_root=Path("/cache"),
            source_cache_root=Path("/") / AGENT_SOURCE_CACHE_RELATIVE_PATH,
            checkpoint_root="/checkpoints",
        ),
        monitoring=WorkerMonitoringConfiguration(
            metrics_interval_seconds=3.0,
        ),
    )


def plan_worker_container(
    bootstrap: AgentBootstrap,
    slot: AgentWorkerSlot,
    *,
    state_dir: str,
    image: str,
    image_id: str = "",
    target_host: str = "127.0.0.1",
    platform: str = "",
    host_aliases: list[str] | None = None,
    network: AgentWorkerNetwork | None = None,
) -> AgentWorkerContainerPlan:
    selected_network = network or AgentWorkerNetwork()
    dirs = build_agent_worker_dirs(state_dir, slot.worker_id)
    name = f"{AGENT_NAME}-{sanitize_worker_name(slot.worker_id)}"
    config_path = posixpath.join(dirs.slot, "worker.yaml")
    container_service_port = agent_worker_container_service_port(slot.worker_id)
    labels = {
        AGENT_MANAGED_LABEL: "true",
        AGENT_WORKER_ID_LABEL: slot.worker_id,
        f"{NAME}.agent.machine_id": slot.machine_id,
        f"{NAME}.agent.pool_name": str(slot.pool),
    }
    if image_id:
        labels[f"{NAME}.agent.worker_image_id"] = image_id
    gateway_env = agent_gateway_env(bootstrap)
    env = {
        WORKER_CONFIG_PATH_ENV: DEFAULT_WORKER_CONFIG_PATH,
        "WORKER_ID": slot.worker_id,
        "WORKER_TOKEN": slot.worker_token,
        "WORKER_POOL": str(slot.pool),
        "WORKER_CAPACITY_OWNER_ID": slot.capacity_owner_id,
        "WORKER_MACHINE": slot.machine_id,
        "WORKER_POD_ADDRESS": target_host,
        "WORKER_CONTAINER_SERVICE_PORT": str(container_service_port),
        "CACHE_LOCALITY": str(slot.pool),
        "CACHE_NODE": slot.machine_id,
        "WORKER_SOURCE_CACHE_STORAGE_ID": f"machine:{slot.machine_id}",
        "WORKER_NETWORK_PREFIX": slot.network_prefix,
        "WORKER_ROUTE_TARGET": target_host,
        # The same origin the worker's gateway client uses. Computing it a second
        # time here is what let the two disagree: one honoured the agent's
        # runtime-URL override and the other did not, so the worker held a
        # reachable gateway and an unreachable repository and never reported
        # itself available.
        WORKER_REPOSITORY_URL_ENV: gateway_env[GATEWAY_HTTP_URL_ENV],
    }
    if slot.gpu_count > 0:
        assignment = slot.gpu_assignment or "all"
        env["NVIDIA_VISIBLE_DEVICES"] = assignment
        env["WORKER_GPU_DEVICES"] = assignment
    env.update(gateway_env)
    volumes = [
        f"{dirs.images}:/images",
        f"{dirs.tmp}:{AGENT_CONTAINER_TMP_PATH}",
        f"{dirs.data}:{AGENT_CONTAINER_DATA_PATH}",
        f"{dirs.workspace}:/workspace",
        f"{dirs.cache}:/cache",
        f"{dirs.builds}:/builds",
        f"{dirs.checkpoints}:/checkpoints",
        f"{dirs.logs}:{AGENT_CONTAINER_LOG_PATH}",
        f"{config_path}:{DEFAULT_WORKER_CONFIG_PATH}:ro",
    ]
    docker_args = _worker_docker_args(
        name=name,
        image=image,
        labels=labels,
        env=env,
        volumes=volumes,
        slot=slot,
        platform=platform,
        host_aliases=host_aliases or [],
        network=selected_network,
    )
    return AgentWorkerContainerPlan(
        slot=slot,
        name=name,
        image=image,
        network=selected_network,
        config_path=config_path,
        dirs=dirs,
        labels=labels,
        env=env,
        volumes=volumes,
        docker_args=docker_args,
        config=build_agent_worker_config(bootstrap, slot, network=selected_network),
    )


def agent_worker_container_service_port(worker_id: str) -> int:
    digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % AGENT_WORKER_CONTAINER_SERVICE_PORT_SPAN
    return AGENT_WORKER_CONTAINER_SERVICE_BASE_PORT + offset


def same_worker_slot(a: AgentWorkerSlot | None, b: AgentWorkerSlot | None) -> bool:
    if a is None or b is None:
        return a is b
    comparable = [
        "worker_id",
        "pool",
        "machine_id",
        "cpu_millicores",
        "memory_mb",
        "gpu",
        "gpu_count",
        "gpu_assignment",
        "network_prefix",
        "worker_image",
        # A reclassified unit has to restart the worker: the label is stamped on
        # every usage record the running worker emits, and one left running under
        # its old classification keeps billing the wrong way until it exits.
        "billing_owner",
    ]
    return all(getattr(a, field_name) == getattr(b, field_name) for field_name in comparable)


def plan_worker_slot_reconciliation(
    desired_slots: list[AgentWorkerSlot],
    active_slots: list[AgentWorkerSlot],
    *,
    executor: WorkerExecutor = WorkerExecutor.Container,
    os_name: str = "linux",
) -> AgentWorkerReconcilePlan:
    actions: list[AgentWorkerReconcileAction] = []
    active_by_id = {slot.worker_id: slot for slot in active_slots}
    desired_by_id = {slot.worker_id: slot for slot in desired_slots}
    if executor is not WorkerExecutor.Container:
        for active in active_slots:
            actions.append(
                AgentWorkerReconcileAction(
                    action=WorkerSlotAction.Stop,
                    worker_id=active.worker_id,
                    reason="worker-container executor is disabled",
                    slot=active,
                )
            )
        for desired in desired_slots:
            actions.append(
                AgentWorkerReconcileAction(
                    action=WorkerSlotAction.Unsupported,
                    worker_id=desired.worker_id,
                    reason="desired worker slot ignored by external executor",
                    slot=desired,
                )
            )
        return AgentWorkerReconcilePlan(executor=executor, actions=actions)
    if os_name.lower() != "linux":
        for desired in desired_slots:
            actions.append(
                AgentWorkerReconcileAction(
                    action=WorkerSlotAction.Unsupported,
                    worker_id=desired.worker_id,
                    reason="worker-container executor requires Linux",
                    slot=desired,
                )
            )
        for active in active_slots:
            actions.append(
                AgentWorkerReconcileAction(
                    action=WorkerSlotAction.Stop,
                    worker_id=active.worker_id,
                    reason="worker-container executor requires Linux",
                    slot=active,
                )
            )
        return AgentWorkerReconcilePlan(executor=executor, actions=actions)
    for worker_id, desired in sorted(desired_by_id.items()):
        active = active_by_id.get(worker_id)
        if active is None:
            action = WorkerSlotAction.Start
            reason = "worker slot is new"
        elif same_worker_slot(active, desired):
            action = WorkerSlotAction.Keep
            reason = "worker slot is unchanged"
        elif desired.status is AgentWorkerSlotStatus.Draining:
            action = WorkerSlotAction.Prepare
            reason = "worker image is prepared while the current worker drains"
        elif desired.status is AgentWorkerSlotStatus.Pending:
            action = WorkerSlotAction.Restart
            reason = "worker slot is drained and ready to switch"
        elif active.worker_image != desired.worker_image:
            action = WorkerSlotAction.Prepare
            reason = "worker image is awaiting restart authorization"
        else:
            action = WorkerSlotAction.Restart
            reason = "worker slot changed"
        actions.append(
            AgentWorkerReconcileAction(
                action=action,
                worker_id=worker_id,
                reason=reason,
                slot=desired,
            )
        )
    for worker_id, active in sorted(active_by_id.items()):
        if worker_id not in desired_by_id:
            actions.append(
                AgentWorkerReconcileAction(
                    action=WorkerSlotAction.Stop,
                    worker_id=worker_id,
                    reason="worker slot no longer desired",
                    slot=active,
                )
            )
    return AgentWorkerReconcilePlan(executor=executor, actions=actions)


def _capacity_check(
    name: AgentCapacityCheckName,
    ok: bool,
    message: str,
) -> AgentCapacityCheck:
    return AgentCapacityCheck(
        name=name,
        ok=ok,
        message=message,
        severity=PreflightSeverity.Info if ok else PreflightSeverity.Error,
    )


def _worker_docker_args(
    *,
    name: str,
    image: str,
    labels: dict[str, str],
    env: dict[str, str],
    volumes: list[str],
    slot: AgentWorkerSlot,
    platform: str,
    host_aliases: list[str],
    network: AgentWorkerNetwork,
) -> list[str]:
    # No `--rm`: a worker that cannot reach the control plane says so on stderr
    # and exits, and self-removal would delete that account in the same instant.
    # The agent reads the exit and the log off the stopped container, then
    # removes it.
    args = [
        "run",
        "--name",
        name,
        "--privileged",
        "--network",
        network.name,
        "--cgroupns",
        "host",
        "--tmpfs",
        (
            "/dev/shm:rw,exec,nosuid,nodev,size="
            f"{max(slot.memory_mb // 2, 64) if slot.memory_mb > 0 else 64}m"
        ),
    ]
    for key, value in sorted(labels.items()):
        args.extend(["--label", f"{key}={value}"])
    if platform:
        args.extend(["--platform", platform])
    for alias in host_aliases:
        args.extend(["--add-host", alias])
    if slot.cpu_millicores > 0:
        args.extend(["--cpus", f"{slot.cpu_millicores / 1000:.3f}"])
    if slot.memory_mb > 0:
        args.extend(["--memory", f"{slot.memory_mb}m"])
    if slot.gpu_count > 0:
        args.extend(["--gpus", f"device={slot.gpu_assignment}" if slot.gpu_assignment else "all"])
    for volume in volumes:
        args.extend(["-v", volume])
    for key, value in sorted(env.items()):
        args.extend(["-e", f"{key}={value}"])
    args.extend([image, CONTAINER_WORKER_PROCESS_NAME])
    return args

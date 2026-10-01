#!/bin/sh
# Installs the LazyCloud agent on a Linux host and joins it to the control
# plane. The server serving this script fills in the release it targets.
#
#   curl -fsSL "$GATEWAY/install/agent" | sh -s -- \
#     --gateway "$GATEWAY" --server host:port --join-token TOKEN [--background]
#
# A release unpacks into <root>/releases/<version> and <root>/current links
# to it. Root installs use /opt/lazycloud/agent with state in
# /var/lib/lazycloud/agent; other users install under ~/.lazycloud/agent.
set -eu

CONFIGURED_VERSION="__AGENT_VERSION__"
CONFIGURED_AMD64_SHA256="__AGENT_AMD64_SHA256__"
CONFIGURED_ARM64_SHA256="__AGENT_ARM64_SHA256__"
READY_TIMEOUT_SECONDS=180

GATEWAY=""
SERVER=""
SERVER_PLAINTEXT=0
SERVER_CA=""
JOIN_TOKEN=""
CLOUD_HOST_ID=""
AGENT_VERSION=""
AGENT_SHA256=""
AGENT_AMD64_SHA256=""
AGENT_ARM64_SHA256=""
BACKGROUND=0
SERVICE_MANAGER="auto"
SERVICE_NAME="lazycloud-agent"
STATE_DIR=""
MAX_CPU=""
MAX_MEMORY=""
MAX_GPUS=""
GPU_IDS=""
AGENT_HOSTNAME=""
ARCH=""
ROOT=""

main() {
  parse_args "$@"
  detect_platform
  validate
  resolve_release
  if [ "$BACKGROUND" = 1 ]; then
    require_service_host
  fi
  require_docker
  choose_paths
  install_release

  set -- --server "$SERVER" --state-dir "$STATE_DIR"
  [ "$SERVER_PLAINTEXT" = 0 ] || set -- "$@" --server-plaintext
  [ -z "$SERVER_CA" ] || set -- "$@" --server-ca "$SERVER_CA"
  [ -z "$JOIN_TOKEN" ] || set -- "$@" --join-token "$JOIN_TOKEN"
  [ -z "$CLOUD_HOST_ID" ] || set -- "$@" --cloud-host-id "$CLOUD_HOST_ID"
  [ -z "$MAX_CPU" ] || set -- "$@" --max-cpu "$MAX_CPU"
  [ -z "$MAX_MEMORY" ] || set -- "$@" --max-memory "$MAX_MEMORY"
  [ -z "$MAX_GPUS" ] || set -- "$@" --max-gpus "$MAX_GPUS"
  [ -z "$GPU_IDS" ] || set -- "$@" --gpu-ids "$GPU_IDS"
  [ -z "$AGENT_HOSTNAME" ] || set -- "$@" --hostname "$AGENT_HOSTNAME"
  if [ "$BACKGROUND" = 1 ]; then
    install_service "$@"
  else
    run_foreground "$@"
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --gateway) need_value "$@"; GATEWAY="$2"; shift 2 ;;
      --server) need_value "$@"; SERVER="$2"; shift 2 ;;
      --server-plaintext) SERVER_PLAINTEXT=1; shift ;;
      --server-ca) need_value "$@"; SERVER_CA="$2"; shift 2 ;;
      --join-token) need_value "$@"; JOIN_TOKEN="$2"; shift 2 ;;
      --cloud-host-id) need_value "$@"; CLOUD_HOST_ID="$2"; shift 2 ;;
      --agent-version) need_value "$@"; AGENT_VERSION="$2"; shift 2 ;;
      --agent-sha256) need_value "$@"; AGENT_SHA256="$2"; shift 2 ;;
      --agent-amd64-sha256) need_value "$@"; AGENT_AMD64_SHA256="$2"; shift 2 ;;
      --agent-arm64-sha256) need_value "$@"; AGENT_ARM64_SHA256="$2"; shift 2 ;;
      --background) BACKGROUND=1; shift ;;
      --foreground) BACKGROUND=0; shift ;;
      --service-manager) need_value "$@"; SERVICE_MANAGER="$2"; shift 2 ;;
      --service-name) need_value "$@"; SERVICE_NAME="$2"; shift 2 ;;
      --state-dir) need_value "$@"; STATE_DIR="$2"; shift 2 ;;
      --max-cpu) need_value "$@"; MAX_CPU="$2"; shift 2 ;;
      --max-memory) need_value "$@"; MAX_MEMORY="$2"; shift 2 ;;
      --max-gpus) need_value "$@"; MAX_GPUS="$2"; shift 2 ;;
      --gpu-ids) need_value "$@"; GPU_IDS="$2"; shift 2 ;;
      --hostname) need_value "$@"; AGENT_HOSTNAME="$2"; shift 2 ;;
      *) fail "unknown argument: $1" 2 ;;
    esac
  done
}

need_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then
    fail "$1 requires a value" 2
  fi
}

detect_platform() {
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  if [ "$os" != linux ]; then
    fail "unsupported operating system: $os; customer machines require Linux" 1
  fi
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) fail "unsupported architecture: $ARCH; expected amd64 or arm64" 1 ;;
  esac
}

validate() {
  case "$GATEWAY" in
    http://*|https://*) GATEWAY="${GATEWAY%/}" ;;
    '') fail "--gateway is required" 2 ;;
    *) fail "--gateway must start with http:// or https://" 2 ;;
  esac
  [ -n "$SERVER" ] || fail "--server is required" 2
  if [ -n "$JOIN_TOKEN" ] && [ -n "$CLOUD_HOST_ID" ]; then
    fail "--join-token and --cloud-host-id cannot be combined" 2
  fi
  if [ -z "$JOIN_TOKEN" ] && [ -z "$CLOUD_HOST_ID" ]; then
    fail "--join-token or --cloud-host-id is required" 2
  fi
  case "$SERVICE_MANAGER" in
    auto|systemd) ;;
    *) fail "unsupported service manager: $SERVICE_MANAGER; expected auto or systemd" 2 ;;
  esac
  case "$AGENT_VERSION" in
    *[!A-Za-z0-9._+-]*) fail "--agent-version contains invalid characters" 2 ;;
  esac
  case "$AGENT_SHA256" in
    '') ;;
    *[!0-9a-f]*) fail "--agent-sha256 must be a lowercase SHA-256 digest" 2 ;;
    *) [ "${#AGENT_SHA256}" -eq 64 ] || fail "--agent-sha256 must be a lowercase SHA-256 digest" 2 ;;
  esac
}

# placeholder prints its argument unless the server left it unfilled.
placeholder() {
  case "$1" in
    __*__) ;;
    *) printf '%s' "$1" ;;
  esac
}

resolve_release() {
  if [ -z "$AGENT_SHA256" ]; then
    case "$ARCH" in
      amd64) AGENT_SHA256="$AGENT_AMD64_SHA256" ;;
      arm64) AGENT_SHA256="$AGENT_ARM64_SHA256" ;;
    esac
  fi
  if [ -n "$AGENT_VERSION" ]; then
    return
  fi
  AGENT_VERSION="$(placeholder "$CONFIGURED_VERSION")"
  if [ -z "$AGENT_SHA256" ] && [ -n "$AGENT_VERSION" ]; then
    case "$ARCH" in
      amd64) AGENT_SHA256="$(placeholder "$CONFIGURED_AMD64_SHA256")" ;;
      arm64) AGENT_SHA256="$(placeholder "$CONFIGURED_ARM64_SHA256")" ;;
    esac
  fi
}

require_service_host() {
  if [ "$(id -u)" -ne 0 ]; then
    fail "background installation requires root; rerun with sudo or use --foreground" 1
  fi
  if [ ! -d /run/systemd/system ] || ! command -v systemctl >/dev/null 2>&1; then
    fail "systemd is required for background installation; use --foreground on this host" 1
  fi
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    fail "Docker is required; install Docker and check that 'docker info' works for this user" 1
  fi
}

choose_paths() {
  if [ "$(id -u)" -eq 0 ]; then
    ROOT=/opt/lazycloud/agent
    [ -n "$STATE_DIR" ] || STATE_DIR=/var/lib/lazycloud/agent
  else
    [ -n "${HOME:-}" ] || fail "HOME is not set" 1
    ROOT="$HOME/.lazycloud/agent"
    [ -n "$STATE_DIR" ] || STATE_DIR="$HOME/.lazycloud/agent/state"
  fi
}

install_release() {
  for tool in tar gzip; do
    command -v "$tool" >/dev/null 2>&1 || fail "$tool is required to unpack the agent release" 1
  done
  mkdir -p "$ROOT/releases"
  url="$GATEWAY/install/agent/linux/$ARCH"
  [ -z "$AGENT_VERSION" ] || url="$GATEWAY/install/agent/$AGENT_VERSION/linux/$ARCH"
  archive="$(mktemp "$ROOT/releases/.download.XXXXXX")"
  staging="$(mktemp -d "$ROOT/releases/.release.XXXXXX")"
  trap 'rm -rf "$archive" "$staging"' EXIT INT TERM
  say "Downloading lazycloud-agent ${AGENT_VERSION:-latest} for linux/$ARCH"
  download "$url" "$archive" || fail "unable to download the agent release from $url" 1
  if [ -n "$AGENT_SHA256" ] && [ "$(file_sha256 "$archive")" != "$AGENT_SHA256" ]; then
    fail "agent artifact SHA-256 mismatch" 1
  fi
  if ! tar -xzf "$archive" -C "$staging" || [ ! -x "$staging/lazycloud-agent" ]; then
    fail "the agent release from $url could not be unpacked" 1
  fi
  reported="$("$staging/lazycloud-agent" --version)" || fail "the downloaded agent does not run on this host" 1
  if [ -z "$AGENT_VERSION" ]; then
    AGENT_VERSION="$reported"
  fi
  release="$ROOT/releases/$AGENT_VERSION"
  if [ -e "$release" ]; then
    doomed="$ROOT/releases/.removing.$$"
    mv "$release" "$doomed"
    rm -rf "$doomed"
  fi
  mv "$staging" "$release"
  rm -f "$archive"
  trap - EXIT INT TERM
  current="$(readlink "$ROOT/current" 2>/dev/null || true)"
  if [ -n "$current" ] && [ "$current" != "releases/$AGENT_VERSION" ]; then
    ln -sfn "$current" "$ROOT/previous.new"
    mv -T "$ROOT/previous.new" "$ROOT/previous"
  fi
  ln -sfn "releases/$AGENT_VERSION" "$ROOT/current.new"
  mv -T "$ROOT/current.new" "$ROOT/current"
  say "Installed lazycloud-agent $AGENT_VERSION in $ROOT"
}

download() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$2" "$1"
  else
    fail "curl or wget is required to download the agent" 1
  fi
}

file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    fail "sha256sum or shasum is required to verify the agent" 1
  fi
}

# run_foreground replaces this shell with the agent.
run_foreground() {
  say "Starting lazycloud-agent"
  exec "$ROOT/current/lazycloud-agent" join "$@" \
    --runtime-dir "$ROOT/current/runtime" --supervisor "$ROOT/current/supervisor"
}

# install_service has the agent install its systemd unit, then waits until
# the host has enrolled and the unit runs.
install_service() {
  say "Installing the $SERVICE_NAME service"
  "$ROOT/current/lazycloud-agent" install-service --service-name "$SERVICE_NAME" "$@" ||
    fail "installing the $SERVICE_NAME service failed" 1
  say "Waiting for $SERVICE_NAME to join"
  elapsed=0
  while [ "$elapsed" -lt "$READY_TIMEOUT_SECONDS" ]; do
    if [ -s "$STATE_DIR/identity.json" ] && systemctl is-active --quiet "$SERVICE_NAME.service"; then
      say "$SERVICE_NAME joined and is running"
      return
    fi
    sleep 2
    elapsed=$((elapsed + 2))
  done
  say "$SERVICE_NAME did not join within ${READY_TIMEOUT_SECONDS}s; recent service log:"
  journalctl --unit "$SERVICE_NAME.service" --no-pager --output cat --lines 50 >&2 2>/dev/null || true
  fail "$SERVICE_NAME failed to join" 1
}

say() {
  printf '=> %s\n' "$1" >&2
}

fail() {
  printf 'error: %s\n' "$1" >&2
  exit "$2"
}

main "$@"

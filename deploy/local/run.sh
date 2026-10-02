#!/bin/sh
# Builds and runs the server, scheduler and agent against compose.yaml.
# Usage: deploy/local/run.sh start|stop. State, logs and credentials live in
# .lazycloud/.
set -eu
cd "$(dirname "$0")/../.."
state=.lazycloud
export GOTOOLCHAIN=go1.27.1
export LAZYCLOUD_DATABASE_URL="postgres://lazycloud:lazycloud@127.0.0.1:25432/lazycloud?sslmode=disable"
# Presigned URLs name this endpoint, and workload containers send artifact and
# volume bytes to them, so it is the Docker bridge gateway rather than
# loopback. This machine reaches it too.
LAZYCLOUD_DOCKER_BRIDGE_IP=$(docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}')
export LAZYCLOUD_DOCKER_BRIDGE_IP
export LAZYCLOUD_OBJECT_STORE_ENDPOINT="http://$LAZYCLOUD_DOCKER_BRIDGE_IP:23900"
export LAZYCLOUD_OBJECT_STORE_REGION=garage
export LAZYCLOUD_OBJECT_STORE_BUCKET=lazycloud
export LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID=GK1a2b3c4d5e6f708192a3b4c5
export LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY=6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31
export LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER=garage
export LAZYCLOUD_GARAGE_ADMIN_URL=http://127.0.0.1:23903
export LAZYCLOUD_GARAGE_ADMIN_TOKEN=local-garage-admin
# The master key that wraps secret data keys; generated once per state dir.
export LAZYCLOUD_SECRETS_KEY_FILE="$PWD/$state/secrets.key"
# Local callbacks may target this machine.
export LAZYCLOUD_CALLBACK_ALLOW_PRIVATE=1
export LAZYCLOUD_IMAGE_REGISTRY=127.0.0.1:25000
export LAZYCLOUD_IMAGE_REGISTRY_INSECURE=true
# Agent release archives `lazycloud machine join` installs from.
export LAZYCLOUD_AGENT_DIST_DIR="$PWD/$state/agent-dist"

stop() {
  for name in agent scheduler server; do
    if [ -f "$state/$name.pid" ]; then
      kill "$(cat "$state/$name.pid")" 2>/dev/null || true
      rm -f "$state/$name.pid"
    fi
  done
}

start() {
  mkdir -p "$state/logs" bin
  if [ ! -f "$LAZYCLOUD_SECRETS_KEY_FILE" ]; then
    (umask 077 && head -c 32 /dev/urandom >"$LAZYCLOUD_SECRETS_KEY_FILE")
  fi
  docker compose up -d --wait postgres object-store registry >/dev/null
  docker compose run --rm object-store-bootstrap >/dev/null
  CGO_ENABLED=0 go build -o bin/supervisor ./cmd/supervisor
  go build -o bin/server ./cmd/server
  go build -o bin/scheduler ./cmd/scheduler
  go build -o bin/agent ./cmd/agent
  deploy/local/fetch-geesefs.sh
  # Rebuild the managed runtime whenever the Python it bundles changes; a stale
  # runtime silently lacks newer runner features.
  digest=$(find python/lazycloud/src python/runner/src uv.lock -type f \
    -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
  if [ ! -d "$state/runtime/3.12" ] || [ "$(cat "$state/runtime/.source-digest" 2>/dev/null)" != "$digest" ]; then
    deploy/local/build-runtime.sh "$state/runtime"
    echo "$digest" >"$state/runtime/.source-digest"
  fi

  bin/server migrate
  # This tree's agent is the release joined machines install and update to.
  release="local-$(sha256sum bin/agent | cut -c1-12)"
  [ -f "$LAZYCLOUD_AGENT_DIST_DIR/$release/lazycloud-agent-linux-amd64.tar.gz" ] ||
    deploy/agent/build-bundle.sh --python 3.12 "$LAZYCLOUD_AGENT_DIST_DIR" "$release" >/dev/null
  bin/server admin publish-agent-release -version "$release" -dist "$LAZYCLOUD_AGENT_DIST_DIR" >/dev/null
  if [ ! -f "$state/token" ]; then
    bin/server admin create-user --email dev@lazycloud.local --admin >/dev/null
    bin/server admin create-workspace --name dev --owner-email dev@lazycloud.local >/dev/null
    bin/server admin create-token --email dev@lazycloud.local --name dev >"$state/token"
  fi
  # The local stack takes no payments, so its development account is waived.
  bin/server admin set-complimentary --email dev@lazycloud.local
  [ -f "$state/agent/identity.json" ] || bin/server admin create-join-token >"$state/join-token"

  bin/server serve >"$state/logs/server.log" 2>&1 &
  echo $! >"$state/server.pid"
  bin/scheduler >"$state/logs/scheduler.log" 2>&1 &
  echo $! >"$state/scheduler.pid"
  join=""
  [ -f "$state/join-token" ] && join=$(cat "$state/join-token")
  # LAZYCLOUD_OCI_RUNTIME=runsc uses gVisor (deploy/local/host-setup.sh installs
  # it). Devbox disks and snapshots need a root agent: with
  # LAZYCLOUD_AGENT_AS_ROOT=1 the agent is not started here, and the command to
  # start it with sudo in another terminal is printed instead.
  runtime="${LAZYCLOUD_OCI_RUNTIME:-runc}"
  if [ "${LAZYCLOUD_AGENT_AS_ROOT:-}" = 1 ]; then
    echo "start the agent as root in another terminal:"
    echo "  sudo $PWD/bin/agent join -server 127.0.0.1:8081 -server-plaintext -join-token '$join' -state-dir '$PWD/$state/agent' -runtime-dir '$PWD/$state/runtime' -supervisor '$PWD/bin/supervisor' -geesefs '$PWD/bin/geesefs' -oci-runtime '$runtime' -build-network host"
  else
    bin/agent join -server 127.0.0.1:8081 -server-plaintext -join-token "$join" -state-dir "$PWD/$state/agent" \
    -runtime-dir "$PWD/$state/runtime" -supervisor "$PWD/bin/supervisor" -geesefs "$PWD/bin/geesefs" -oci-runtime "$runtime" -build-network host \
      >"$state/logs/agent.log" 2>&1 &
    echo $! >"$state/agent.pid"
  fi

  echo "API http://127.0.0.1:8080, workspace dev; workloads answer under http://<host>.lazycloud.localhost:8082"
  echo "export LAZYCLOUD_ENDPOINT=http://127.0.0.1:8080 LAZYCLOUD_WORKSPACE=dev LAZYCLOUD_TOKEN=$(cat "$state/token")"
}

case "${1:-}" in
  start) stop; start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

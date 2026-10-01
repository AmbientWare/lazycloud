#!/bin/sh
# Builds and runs the server, scheduler and agent against compose.yaml.
# Usage: deploy/local/run.sh start|stop. State, logs and credentials live in
# .lazycloud/.
set -eu
cd "$(dirname "$0")/../.."
state=.lazycloud
export GOTOOLCHAIN=go1.27.1
export LAZYCLOUD_DATABASE_URL="postgres://lazycloud:lazycloud@127.0.0.1:25432/lazycloud?sslmode=disable"
export LAZYCLOUD_OBJECT_STORE_ENDPOINT=http://127.0.0.1:23900
export LAZYCLOUD_OBJECT_STORE_REGION=garage
export LAZYCLOUD_OBJECT_STORE_BUCKET=lazycloud
export LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID=GK1a2b3c4d5e6f708192a3b4c5
export LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY=6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31
# The master key that wraps secret data keys; generated once per state dir.
export LAZYCLOUD_SECRETS_KEY_FILE="$PWD/$state/secrets.key"
# Local callbacks may target this machine.
export LAZYCLOUD_CALLBACK_ALLOW_PRIVATE=1
export LAZYCLOUD_IMAGE_REGISTRY=127.0.0.1:25000
export LAZYCLOUD_IMAGE_REGISTRY_INSECURE=true

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
  [ -d "$state/runtime/3.12" ] || deploy/local/build-runtime.sh "$state/runtime"

  bin/server migrate
  if [ ! -f "$state/token" ]; then
    bin/server admin create-user --email dev@lazycloud.local --admin >/dev/null
    bin/server admin create-workspace --name dev --owner-email dev@lazycloud.local >/dev/null
    bin/server admin create-token --email dev@lazycloud.local --name dev >"$state/token"
  fi
  [ -f "$state/agent/identity.json" ] || bin/server admin create-join-token >"$state/join-token"

  bin/server serve >"$state/logs/server.log" 2>&1 &
  echo $! >"$state/server.pid"
  bin/scheduler >"$state/logs/scheduler.log" 2>&1 &
  echo $! >"$state/scheduler.pid"
  join=""
  [ -f "$state/join-token" ] && join=$(cat "$state/join-token")
  bin/agent -server 127.0.0.1:8081 -join-token "$join" -state-dir "$PWD/$state/agent" \
    -runtime-dir "$PWD/$state/runtime" -supervisor "$PWD/bin/supervisor" -oci-runtime runc -build-network host \
    >"$state/logs/agent.log" 2>&1 &
  echo $! >"$state/agent.pid"

  echo "API http://127.0.0.1:8080, workspace dev"
  echo "export LAZYCLOUD_ENDPOINT=http://127.0.0.1:8080 LAZYCLOUD_WORKSPACE=dev LAZYCLOUD_TOKEN=$(cat "$state/token")"
}

case "${1:-}" in
  start) stop; start ;;
  stop) stop ;;
  *) echo "usage: $0 start|stop" >&2; exit 2 ;;
esac

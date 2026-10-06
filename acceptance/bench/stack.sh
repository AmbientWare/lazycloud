#!/bin/sh
# deploy/local/run.sh on benchmark ports, Compose project and state directory,
# so it runs beside other local stacks. Builds the tree at $LCBENCH_TREE, by
# default the checkout holding this script.
# Usage: stack.sh start|stop|agent-stop|agent-start|scheduler2|scheduler2-stop|down
set -eu
bench=$(cd "$(dirname "$0")" && pwd)
cd "${LCBENCH_TREE:-$bench/../..}"
state=${LCBENCH_STATE:-/tmp/lcbench/stack}
compose="docker compose -p lcbench -f compose.yaml -f $bench/compose.bench.yaml"
export GOTOOLCHAIN=go1.27.1
export LAZYCLOUD_DATABASE_URL="postgres://lazycloud:lazycloud@127.0.0.1:26432/lazycloud?sslmode=disable"
export LAZYCLOUD_OBJECT_STORE_ENDPOINT=http://127.0.0.1:26900
export LAZYCLOUD_OBJECT_STORE_REGION=garage
export LAZYCLOUD_OBJECT_STORE_BUCKET=lazycloud
export LAZYCLOUD_OBJECT_STORE_LAYER_BUCKET=lazycloud-layers
export LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID=GK1a2b3c4d5e6f708192a3b4c5
export LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY=6c6f63616c2d6c617a79636c6f75642d6465762d7365637265742d6b65792d31
export LAZYCLOUD_WORKSPACE_BUCKET_PROVIDER=garage
export LAZYCLOUD_GARAGE_ADMIN_URL=http://127.0.0.1:26903
export LAZYCLOUD_GARAGE_ADMIN_TOKEN=local-garage-admin
export LAZYCLOUD_SECRETS_KEY_FILE="$state/secrets.key"
export LAZYCLOUD_CALLBACK_ALLOW_PRIVATE=1
export LAZYCLOUD_IMAGE_REGISTRY=127.0.0.1:26000
export LAZYCLOUD_IMAGE_REGISTRY_INSECURE=true
export LAZYCLOUD_AGENT_DIST_DIR="$state/agent-dist"
export LAZYCLOUD_HTTP_ADDR=127.0.0.1:28080
export LAZYCLOUD_GRPC_ADDR=127.0.0.1:28081
export LAZYCLOUD_EDGE_ADDR=127.0.0.1:28082
export LAZYCLOUD_EDGE_RELAY_ADDR=127.0.0.1:28083
export LAZYCLOUD_EDGE_URL=http://lazycloud.localhost:28082
export LAZYCLOUD_PUBLIC_URL=http://127.0.0.1:28080

stop() {
  for name in agent scheduler2 scheduler server; do
    if [ -f "$state/$name.pid" ]; then
      kill "$(cat "$state/$name.pid")" 2>/dev/null || true
      rm -f "$state/$name.pid"
    fi
  done
}

agent() {
  join=""
  [ -f "$state/join-token" ] && join=$(cat "$state/join-token")
  bin/agent join -server 127.0.0.1:28081 -server-plaintext -join-token "$join" -state-dir "$state/agent" \
    -runtime-dir "$state/runtime" -supervisor "$PWD/bin/supervisor" -geesefs "$PWD/bin/geesefs" -oci-runtime runc -build-network host \
    -max-cpu "${LCBENCH_MAX_CPU:-4}" -max-memory "${LCBENCH_MAX_MEMORY:-8gib}" \
    >>"$state/logs/agent.log" 2>&1 &
  echo $! >"$state/agent.pid"
}

start() {
  mkdir -p "$state/logs" bin
  if [ ! -f "$LAZYCLOUD_SECRETS_KEY_FILE" ]; then
    (umask 077 && head -c 32 /dev/urandom >"$LAZYCLOUD_SECRETS_KEY_FILE")
  fi
  $compose up -d --wait postgres object-store registry >/dev/null
  $compose run --rm object-store-bootstrap >/dev/null
  CGO_ENABLED=0 go build -o bin/supervisor ./cmd/supervisor
  go build -o bin/server ./cmd/server
  go build -o bin/scheduler ./cmd/scheduler
  go build -o bin/agent ./cmd/agent
  deploy/local/fetch-geesefs.sh
  [ -d "$state/runtime/3.12" ] || deploy/local/build-runtime.sh "$state/runtime" 3.12

  bin/server migrate
  # The samplers read query counts from it; the preloaded library alone
  # does not create the view.
  $compose exec -T postgres psql -U lazycloud -d lazycloud -qc 'create extension if not exists pg_stat_statements'
  release="local-$(sha256sum bin/agent | cut -c1-12)"
  [ -f "$LAZYCLOUD_AGENT_DIST_DIR/$release/lazycloud-agent-linux-amd64.tar.gz" ] ||
    deploy/agent/build-bundle.sh --python 3.12 "$LAZYCLOUD_AGENT_DIST_DIR" "$release" >/dev/null
  bin/server admin publish-agent-release -version "$release" -dist "$LAZYCLOUD_AGENT_DIST_DIR" >/dev/null
  if [ ! -f "$state/token" ]; then
    bin/server admin create-user --email dev@lazycloud.local --admin >/dev/null
    # Complimentary first: its entitlements allow the second workspace.
    bin/server admin set-complimentary --email dev@lazycloud.local
    bin/server admin create-workspace --name dev --owner-email dev@lazycloud.local >/dev/null
    # The backlog phase measures fairness between two workspaces.
    bin/server admin create-workspace --name dev2 --owner-email dev@lazycloud.local >/dev/null
    bin/server admin create-token --email dev@lazycloud.local --name dev >"$state/token"
  fi
  [ -f "$state/agent/identity.json" ] || bin/server admin create-join-token >"$state/join-token"

  bin/server serve >"$state/logs/server.log" 2>&1 &
  echo $! >"$state/server.pid"
  bin/scheduler >"$state/logs/scheduler.log" 2>&1 &
  echo $! >"$state/scheduler.pid"
  agent
  echo "export LAZYCLOUD_ENDPOINT=http://127.0.0.1:28080 LAZYCLOUD_WORKSPACE=dev LAZYCLOUD_TOKEN=$(cat "$state/token")"
}

case "${1:-}" in
  start) stop; start ;;
  stop) stop ;;
  agent-stop) kill "$(cat "$state/agent.pid")"; rm -f "$state/agent.pid" ;;
  agent-start) agent ;;
  scheduler2-stop) [ ! -f "$state/scheduler2.pid" ] || { kill "$(cat "$state/scheduler2.pid")" || true; rm -f "$state/scheduler2.pid"; } ;;
  scheduler2)
    bin/scheduler >"$state/logs/scheduler2.log" 2>&1 &
    echo $! >"$state/scheduler2.pid" ;;
  down)
    stop
    # A stopped agent leaves its workload containers running; remove this
    # stack's host's containers with the stack.
    if [ -f "$state/agent/identity.json" ]; then
      host=$(sed -n 's/.*"host_id": *"\([^"]*\)".*/\1/p' "$state/agent/identity.json")
      [ -z "$host" ] || docker ps -aq --filter "label=lazycloud.host-id=$host" | xargs -r docker rm -f >/dev/null
    fi
    $compose down -v ;;
  *) echo "usage: $0 start|stop|agent-stop|agent-start|scheduler2|scheduler2-stop|down" >&2; exit 2 ;;
esac

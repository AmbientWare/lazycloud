#!/bin/bash
# The acceptance tests as CI's acceptance job runs them, on a machine set up
# as a host (install-snapshotter.sh and gVisor's runsc with --host-uds=all)
# with the compose test stack reachable.
#
# Usage: deploy/local/acceptance.sh prepare|run [go test args]
#   prepare  install GeeseFS, build the managed runtime and build the agent
#            release this change replaces into .lazycloud/previous-agent:
#            the merge base with origin/main, or on main the commit before,
#            which hosts run until this one ships. Needs git, Go and uv.
#   run      go test -race ./acceptance as root, as hosts run the agent, with
#            workloads under runsc; a skipped test fails the run
set -euo pipefail
cd "$(dirname "$0")/../.."
previous=$PWD/.lazycloud/previous-agent

case "${1:-}" in
  prepare)
    deploy/local/fetch-geesefs.sh
    deploy/local/build-runtime.sh .lazycloud/runtime 3.12
    base=$(git merge-base HEAD origin/main)
    if [ "$base" = "$(git rev-parse HEAD)" ]; then base=$(git rev-parse HEAD^); fi
    if [ "$(cat "$previous/base" 2>/dev/null)" != "$base" ]; then
      tree=$(mktemp -d)
      trap 'rm -rf "$tree"' EXIT
      git archive "$base" | tar -x -C "$tree"
      rm -rf "$previous"
      CGO_ENABLED=0 go build -C "$tree" -trimpath -ldflags "-X main.version=previous-${base:0:12}" \
        -o "$previous/lazycloud-agent" ./cmd/agent
      CGO_ENABLED=0 go build -C "$tree" -trimpath -o "$previous/supervisor" ./cmd/supervisor
      echo "$base" >"$previous/base"
    fi
    ;;
  run)
    shift
    log=$(mktemp)
    trap 'rm -f "$log"' EXIT
    root=()
    if [ "$(id -u)" -ne 0 ]; then root=(sudo "--preserve-env=HOME,GOPATH,GOCACHE,GOMODCACHE,GOTOOLCHAIN,LAZYCLOUD_OTLP_ENDPOINT,LAZYCLOUD_OTLP_INSECURE"); fi
    "${root[@]}" env PATH="$PATH" LAZYCLOUD_TEST_OCI_RUNTIME=runsc LAZYCLOUD_TEST_OLD_AGENT="$previous" \
      go test -race -timeout 60m -v ./acceptance "$@" 2>&1 | tee "$log"
    if grep -- '--- SKIP' "$log"; then
      echo "acceptance tests skipped" >&2
      exit 1
    fi
    ;;
  *) echo "usage: $0 prepare|run [go test args]" >&2; exit 2 ;;
esac

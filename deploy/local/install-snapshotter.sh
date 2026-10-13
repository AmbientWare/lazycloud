#!/bin/sh
# Sets this machine up as a host for the agent tests, which run as root:
# builds the agent and the snapshotter and installs the snapshotter as hosts
# do, caching volumes as on GPU hosts. With LAZYCLOUD_OTLP_ENDPOINT set, the
# snapshotter traces there.
set -eu
cd "$(dirname "$0")/../.."
release=$(mktemp -d)
trap 'rm -rf "$release"' EXIT
go build -o "$release/lazycloud-agent" ./cmd/agent
go build -o "$release/lazycloud-snapshotter" ./cmd/snapshotter
sudo mkdir -p /etc/systemd/system/lazycloud-snapshotter.service.d
printf '[Service]\nEnvironment=LAZYCLOUD_VOLUME_CACHE=true\n' |
  sudo tee /etc/systemd/system/lazycloud-snapshotter.service.d/volume-cache.conf >/dev/null
if [ -n "${LAZYCLOUD_OTLP_ENDPOINT:-}" ]; then
  printf '[Service]\nEnvironment=LAZYCLOUD_OTLP_ENDPOINT=%s LAZYCLOUD_OTLP_INSECURE=true\n' "$LAZYCLOUD_OTLP_ENDPOINT" |
    sudo tee /etc/systemd/system/lazycloud-snapshotter.service.d/traces.conf >/dev/null
fi
sudo "$release/lazycloud-agent" install-snapshotter
docker info --format 'storage driver {{.Driver}}'

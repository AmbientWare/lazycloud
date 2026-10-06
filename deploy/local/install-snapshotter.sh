#!/bin/sh
# Sets this machine up as a host for the snapshotter tests: builds the agent
# and the snapshotter, installs the snapshotter as hosts do and lets the
# docker group reach its socket and containerd's, since the tests run as a
# user in that group. With LAZYCLOUD_OTLP_ENDPOINT set, the snapshotter
# traces there.
set -eu
cd "$(dirname "$0")/../.."
release=$(mktemp -d)
trap 'rm -rf "$release"' EXIT
go build -o "$release/lazycloud-agent" ./cmd/agent
go build -o "$release/lazycloud-snapshotter" ./cmd/snapshotter
if [ -n "${LAZYCLOUD_OTLP_ENDPOINT:-}" ]; then
  sudo mkdir -p /etc/systemd/system/lazycloud-snapshotter.service.d
  printf '[Service]\nEnvironment=LAZYCLOUD_OTLP_ENDPOINT=%s LAZYCLOUD_OTLP_INSECURE=true\n' "$LAZYCLOUD_OTLP_ENDPOINT" |
    sudo tee /etc/systemd/system/lazycloud-snapshotter.service.d/traces.conf >/dev/null
fi
sudo "$release/lazycloud-agent" install-snapshotter
sudo chmod 0755 /run/lazycloud-snapshotter
sudo chgrp docker /run/lazycloud-snapshotter/snapshotter.sock /run/containerd/containerd.sock
sudo chmod g+rw /run/lazycloud-snapshotter/snapshotter.sock /run/containerd/containerd.sock
docker info --format 'storage driver {{.Driver}}'

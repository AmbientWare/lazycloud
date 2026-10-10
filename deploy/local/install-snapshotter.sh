#!/bin/sh
# Sets this machine up as a host for the agent tests: builds the agent and
# the snapshotter, installs the snapshotter as hosts do and lets the docker
# group reach its socket and containerd's and make the systemd slices a root
# agent makes for containers with volumes, since the tests run as a user in
# that group. With LAZYCLOUD_OTLP_ENDPOINT set, the snapshotter traces there.
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
if [ ! -d /etc/polkit-1/rules.d ]; then
  sudo apt-get update -q >/dev/null && sudo apt-get install -y -q polkitd >/dev/null
fi
printf '%s\n' 'polkit.addRule(function (action, subject) {' \
  '  if (action.id == "org.freedesktop.systemd1.manage-units" && subject.isInGroup("docker")) {' \
  '    return polkit.Result.YES;' '  }' '});' |
  sudo tee /etc/polkit-1/rules.d/50-lazycloud-agent-tests.rules >/dev/null
docker info --format 'storage driver {{.Driver}}'

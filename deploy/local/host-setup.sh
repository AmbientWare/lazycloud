#!/bin/sh
# One-time host setup for running gVisor, devbox disks and memory snapshots
# locally, the same pieces production hosts carry. Run with sudo:
#   sudo deploy/local/host-setup.sh
# It installs runsc (checksum verified), registers it as the Docker runtime
# `runsc` beside existing runtimes, reloads Docker without restarting running
# containers, installs nbd-client and loads the nbd module now and at boot.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

arch=$(uname -m)
base="https://storage.googleapis.com/gvisor/releases/release/latest/${arch}"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for file in runsc runsc.sha512 containerd-shim-runsc-v1 containerd-shim-runsc-v1.sha512; do
  curl -fsSL -o "$work/$file" "$base/$file"
done
(cd "$work" && sha512sum -c runsc.sha512 && sha512sum -c containerd-shim-runsc-v1.sha512)
install -m 0755 "$work/runsc" "$work/containerd-shim-runsc-v1" /usr/local/bin/
/usr/local/bin/runsc --version | head -1

# The agent serves and dials Unix sockets inside bind mounts, so containers
# need host UDS access in both directions.
daemon=/etc/docker/daemon.json
[ -f "$daemon" ] || echo '{}' >"$daemon"
cp "$daemon" "$daemon.bak.lazycloud"
python3 - "$daemon" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    config = json.load(f)
config.setdefault("runtimes", {})["runsc"] = {
    "path": "/usr/local/bin/runsc",
    "runtimeArgs": ["--host-uds=all"],
}
with open(path, "w") as f:
    json.dump(config, f, indent=4)
    f.write("\n")
PY
# Runtimes are reloadable; SIGHUP keeps running containers up.
systemctl reload docker
sleep 2
docker info --format '{{json .Runtimes}}' | grep -q '"runsc"' && echo "docker runtime runsc registered"

apt-get install -y nbd-client >/dev/null
modprobe nbd max_part=8
echo "nbd" >/etc/modules-load.d/lazycloud-nbd.conf
echo "options nbd max_part=8" >/etc/modprobe.d/lazycloud-nbd.conf
echo "nbd module loaded: $(ls /dev/nbd0 2>/dev/null || echo missing)"

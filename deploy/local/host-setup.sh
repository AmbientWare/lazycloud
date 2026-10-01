#!/bin/sh
# One-time host setup for running gVisor, devbox disks and memory snapshots
# locally, the same pieces production hosts carry. Run with sudo:
#   sudo deploy/local/host-setup.sh
# It installs gVisor (pinned release, checksum verified), registers it as the Docker runtime
# `runsc` beside existing runtimes, reloads Docker without restarting running
# containers, installs nbd-client and loads the nbd module now and at boot.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

# gVisor ships one tarball per release with runsc, its containerd shim and the
# helper binaries runsc runs from gvisor-bin/ beside itself. The release and
# its digests are pinned.
release=20260928
case "$(uname -m)" in
  x86_64) arch=x86_64 sha=c8d3a9fd4d4c4f5b8ff213caa4517356be128d18659ec4cde37828fe797f61a9725a602a846c81a8ed19c057a996515d31c081eba343ed4613a89951ba32ed59 ;;
  aarch64) arch=aarch64 sha=926538a4f20056d44838f230297ecec9192db2562e2523a207295f706b76126725f2ff7b4e59d747147510c5714057eeec862a0f77d43bf625746592b5f51b00 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/gvisor.tar.bz2" "https://storage.googleapis.com/gvisor/releases/release/${release}/${arch}/gvisor.tar.bz2"
echo "$sha  $work/gvisor.tar.bz2" | sha512sum -c -
dest=/usr/local/lib/gvisor/$release
mkdir -p "$dest"
tar -xjf "$work/gvisor.tar.bz2" -C "$dest"
ln -sf "$dest/runsc" /usr/local/bin/runsc
ln -sf "$dest/containerd-shim-runsc-v1" /usr/local/bin/containerd-shim-runsc-v1
"$dest/runsc" --version | head -1

# The agent serves and dials Unix sockets inside bind mounts, so containers
# need host UDS access in both directions.
daemon=/etc/docker/daemon.json
[ -f "$daemon" ] || echo '{}' >"$daemon"
cp "$daemon" "$daemon.bak.lazycloud"
python3 - "$daemon" "$release" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    config = json.load(f)
config.setdefault("runtimes", {})["runsc"] = {
    "path": "/usr/local/lib/gvisor/%s/runsc" % sys.argv[2],
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

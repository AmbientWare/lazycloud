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
# its digests are pinned in deploy/host-pins.sh, which node images share.
# shellcheck source=SCRIPTDIR/../host-pins.sh
. "$(dirname "$0")/../host-pins.sh"
release=$GVISOR_RELEASE
case "$(uname -m)" in
  x86_64) arch=x86_64 sha=$GVISOR_SHA512_X86_64 ;;
  aarch64) arch=aarch64 sha=$GVISOR_SHA512_AARCH64 ;;
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

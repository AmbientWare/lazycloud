#!/bin/bash
# The fleet node image recipe, run as root on a fresh Amazon Linux 2023
# instance. deploy/ami/bake.sh prepends deploy/host-pins.sh and VARIANT (cpu
# or gpu) and passes the result as user data; it images the instance once
# the console shows LAZYCLOUD_BAKE_OK. The image carries Docker with gVisor's
# runsc as a runtime, the disk engine's tools and the nbd module; the agent
# release arrives at boot through the launcher's user data.
set -Eeuo pipefail

: "${VARIANT:?}" "${GVISOR_RELEASE:?}" "${QEMU_VERSION:?}" "${NBD_VERSION:?}"

# The instance has neither SSH nor an instance profile, so the console is
# the only place its progress shows.
exec > >(tee -a /var/log/lazycloud-bake.log >/dev/console) 2>&1
say() { echo "$*" >/dev/console; }
trap 'say "LAZYCLOUD_BAKE_FAILED line=$LINENO command=$BASH_COMMAND"' ERR

fetch() {
  curl -fsSL --retry 5 -o "$1" "$2"
  echo "$3  $1" | "${4:-sha256sum}" -c -
}

work=$(mktemp -d)
cd "$work"

dnf install -y docker e2fsprogs amazon-ssm-agent
systemctl enable docker amazon-ssm-agent

# qemu-storage-daemon and qemu-img serve and shape disk layers; nbd-client
# attaches them. Built here because Amazon Linux packages neither the daemon
# nor the client; the build tools leave with the sources.
build_deps=(gcc make ninja-build pkgconf-pkg-config glib2-devel zlib-devel python3 python3-tomli bzip2 xz tar flex bison libnl3-devel)
dnf install -y "${build_deps[@]}"
fetch qemu.tar.xz "https://download.qemu.org/qemu-${QEMU_VERSION}.tar.xz" "$QEMU_SHA256"
tar -xJf qemu.tar.xz
(
  cd "qemu-${QEMU_VERSION}"
  ./configure --prefix=/usr/local --target-list= --disable-system --disable-user --disable-docs \
    --disable-werror --enable-tools
  ninja -C build qemu-img storage-daemon/qemu-storage-daemon
  install -m 0755 build/qemu-img build/storage-daemon/qemu-storage-daemon /usr/local/bin/
)
fetch nbd.tar.xz "https://github.com/NetworkBlockDevice/nbd/releases/download/nbd-${NBD_VERSION}/nbd-${NBD_VERSION}.tar.xz" "$NBD_SHA256"
tar -xJf nbd.tar.xz
(
  cd "nbd-${NBD_VERSION}"
  # Its configure refuses --disable-manpages; naming a converter satisfies
  # it, and only the client is built.
  DB2M=true ./configure --prefix=/usr/local --sbindir=/usr/local/sbin
  make -j"$(nproc)" nbd-client
  install -m 0755 nbd-client /usr/local/sbin/nbd-client
)
dnf remove -y gcc ninja-build flex bison glib2-devel zlib-devel libnl3-devel
qemu-storage-daemon --version | head -1
qemu-img --version | head -1
nbd-client --version 2>&1 | head -1 || true

# Disks reach containers through kernel NBD devices, loaded at every boot.
printf 'nbd\n' >/etc/modules-load.d/lazycloud-nbd.conf
printf 'options nbd nbds_max=128 max_part=8\n' >/etc/modprobe.d/lazycloud-nbd.conf
modprobe nbd
test -b /dev/nbd127

if [ "$VARIANT" = gpu ]; then
  # The driver first: the container toolkit configures a Docker runtime that
  # cannot work without it.
  dnf install -y dnf-plugins-core
  dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo
  dnf module enable -y "nvidia-driver:${NVIDIA_DRIVER_STREAM}"
  dnf install -y "nvidia-open-3:${NVIDIA_DRIVER_VERSION}-${NVIDIA_DRIVER_RELEASE}"
  curl -fsSL -o /etc/yum.repos.d/nvidia-container-toolkit.repo \
    https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo
  dnf install -y nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  nvidia-smi -L
fi

# gVisor runs every workload container. The agent serves and dials Unix
# sockets inside bind mounts, so sandboxes need host UDS access both ways;
# GPU sandboxes proxy the driver.
fetch gvisor.tar.bz2 "https://storage.googleapis.com/gvisor/releases/release/${GVISOR_RELEASE}/x86_64/gvisor.tar.bz2" \
  "$GVISOR_SHA512_X86_64" sha512sum
dest=/usr/local/lib/gvisor/$GVISOR_RELEASE
mkdir -p "$dest"
tar -xjf gvisor.tar.bz2 -C "$dest"
ln -sf "$dest/runsc" /usr/local/bin/runsc
ln -sf "$dest/containerd-shim-runsc-v1" /usr/local/bin/containerd-shim-runsc-v1
runsc --version | head -1
runsc_args='["--host-uds=all"]'
if [ "$VARIANT" = gpu ]; then
  driver=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | tr -d '[:space:]')
  if ! runsc nvproxy list-supported-drivers | grep -qx "$driver"; then
    echo "driver $driver is not an ABI gVisor $GVISOR_RELEASE proxies" >&2
    exit 1
  fi
  runsc_args='["--host-uds=all", "--nvproxy"]'
fi
# Hosts offer containers every core. Containers run in lazycloud-workloads.slice,
# and system.slice (the agent, Docker, containerd) outweighs it tenfold for
# CPU, so a container pinning every core cannot starve heartbeats. runsc
# starts the sandbox and gofer inside the container's cgroup, so their CPU
# and memory count against the container; only the shim and runsc's own
# commands run in system.slice.
printf '[Unit]\nDescription=LazyCloud workload containers\n\n[Slice]\nCPUWeight=100\n' \
  >/etc/systemd/system/lazycloud-workloads.slice
mkdir -p /etc/systemd/system/system.slice.d
printf '[Slice]\nCPUWeight=1000\n' >/etc/systemd/system/system.slice.d/lazycloud.conf
systemctl daemon-reload
daemon=/etc/docker/daemon.json
[ -s "$daemon" ] || echo '{}' >"$daemon"
python3 - "$daemon" "$dest/runsc" "$runsc_args" <<'PY'
import json, sys
path, runsc, args = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
with open(path) as f:
    config = json.load(f)
config.setdefault("runtimes", {})["runsc"] = {"path": runsc, "runtimeArgs": args}
config["cgroup-parent"] = "lazycloud-workloads.slice"
with open(path, "w") as f:
    json.dump(config, f, indent=4)
    f.write("\n")
PY
systemctl restart docker
docker info --format '{{json .Runtimes}}' | grep -q '"runsc"'
systemctl show -p CPUWeight system.slice | grep -qx 'CPUWeight=1000'

# The agent's unit (written by its install-service) runs workloads under
# runsc on these hosts.
mkdir -p /etc/systemd/system/lazycloud-agent.service.d
printf '[Service]\nEnvironment=LAZYCLOUD_OCI_RUNTIME=runsc\n' \
  >/etc/systemd/system/lazycloud-agent.service.d/node-image.conf

# A reserve launched able to hibernate writes its memory to a swap file on
# the root volume. hibinit-agent creates the file at each cold boot and puts
# resume=PARTUUID=... resume_offset=... on the boot entry, so the initrd
# finds the root partition whatever order the disks probe in; a release that
# named it by device would resume from whichever disk came first, so the
# bake refuses one. acpid hands EC2's hibernate request to it. The kernel
# writes the smallest image it can, freeing its page cache first.
dnf install -y ec2-hibinit-agent acpid
grep -q PARTUUID /usr/bin/hibinit-agent
systemctl enable hibinit-agent.service acpid.service
printf 'w /sys/power/image_size - - - - 0\n' >/etc/tmpfiles.d/lazycloud-hibernate.conf

# Every boot pays for every boot service.
systemctl mask update-motd.service update-motd.timer systemd-boot-update.service

cat >/etc/lazycloud-node-image.json <<MARKER
{"variant":"$VARIANT","gvisor":"$GVISOR_RELEASE","qemu":"$QEMU_VERSION","nbd":"$NBD_VERSION"}
MARKER
cd /
rm -rf "$work"
dnf clean all
sync
say "LAZYCLOUD_BAKE_OK variant=$VARIANT"

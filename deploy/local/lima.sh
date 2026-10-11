# shellcheck shell=sh
# Lima VMs from Amazon Linux 2023 set up by the node image recipe, so they
# run the same Docker, containerd, gVisor, FUSE and disk tools as fleet
# hosts. host-vm.sh and test-vm.sh source this after setting state, the
# directory Lima, the image and the VMs live in, and vm, the VM's name.
#
# It needs KVM access (the kvm group) and qemu-system-x86_64, not root.
# LAZYCLOUD_LIMA_HOME moves Lima's state, whose socket paths must stay short.

: "${state:?}" "${vm:?}"

LIMA_VERSION=2.2.1
LIMA_SHA256=b391ac7fcac2b2a5a1628107756463188ded4d18d3104a40a66a8bb6fec01bcb
AL2023_RELEASE=2023.12.20260930.0
AL2023_IMAGE=al2023-kvm-$AL2023_RELEASE-kernel-6.1-x86_64.xfs.gpt.qcow2
AL2023_SHA256=aceaf11d27b8265a9aa681448b1e0223db4e3bfcfd13abde40d6654533e5718f

# The VM's disk data volume, attached as its first extra disk (/dev/vdb).
data_disk=$vm-data
lima=$state/lima-$LIMA_VERSION
export LIMA_HOME="${LAZYCLOUD_LIMA_HOME:-$state/lima}"
limactl=$lima/bin/limactl
# Lima's host address in the VM, which reaches this machine's loopback.
host_ip=192.168.5.2

fetch() { # file url sha256
  [ -f "$1" ] && echo "$3  $1" | sha256sum -c --status - && return 0
  curl -fL --retry 3 -o "$1.part" "$2"
  echo "$3  $1.part" | sha256sum -c -
  mv "$1.part" "$1"
}

install_lima() {
  [ -x "$limactl" ] && return 0
  mkdir -p "$lima"
  fetch "$state/lima.tar.gz" "https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION/lima-$LIMA_VERSION-Linux-x86_64.tar.gz" "$LIMA_SHA256"
  tar -xzf "$state/lima.tar.gz" -C "$lima"
  rm -f "$state/lima.tar.gz"
}

exists() { [ -x "$limactl" ] && "$limactl" list --quiet 2>/dev/null | grep -qx "$vm"; }
data_disk_exists() { "$limactl" disk list --json 2>/dev/null | grep -q "\"name\":\"$data_disk\""; }

create() { # cpus memory mounts
  mkdir -p "$state/vm" "$LIMA_HOME"
  fetch "$state/vm/$AL2023_IMAGE" "https://cdn.amazonlinux.com/al2023/os-images/$AL2023_RELEASE/kvm/$AL2023_IMAGE" "$AL2023_SHA256"
  data_disk_exists || "$limactl" disk create "$data_disk" --size "${LAZYCLOUD_VM_DATA_DISK:-64GiB}" --format raw
  cat >"$state/vm/$vm.yaml" <<YAML
vmType: qemu
arch: x86_64
cpus: ${LAZYCLOUD_VM_CPUS:-$1}
memory: ${LAZYCLOUD_VM_MEMORY:-$2}
disk: ${LAZYCLOUD_VM_DISK:-60GiB}
images:
  - location: "$state/vm/$AL2023_IMAGE"
    arch: x86_64
$3
additionalDisks:
  - name: $data_disk
    format: false
containerd:
  system: false
  user: false
YAML
  "$limactl" create --tty=false --name "$vm" "$state/vm/$vm.yaml"
}

# start creates the VM with create's arguments when it is missing and
# starts it when it is stopped.
start() {
  install_lima
  exists || create "$@"
  [ "$("$limactl" list --format '{{.Status}}' "$vm")" = Running ] || "$limactl" start --tty=false "$vm"
}

stop_vm() { if exists; then "$limactl" stop "$vm"; fi; }

delete_vm() {
  [ -x "$limactl" ] || return 0
  if exists; then "$limactl" delete --force "$vm"; fi
  if data_disk_exists; then "$limactl" disk delete "$data_disk"; fi
}

shell() { "$limactl" shell --workdir / "$vm" sudo "$@"; }

# node_image runs the node image recipe once.
node_image() {
  shell test -f /etc/lazycloud-node-image.json && return 0
  # The recipe mounts the data volume a fleet launch maps at /dev/sdf.
  printf 'KERNEL=="vdb", SYMLINK+="sdf"\n' | shell tee /etc/udev/rules.d/90-lazycloud-data.rules >/dev/null
  shell udevadm control --reload
  shell udevadm trigger --action=add /sys/block/vdb
  shell udevadm settle
  { echo "#!/bin/bash"; sed '/^#/d' deploy/host-pins.sh; echo VARIANT=cpu; sed 1d deploy/ami/node-setup.sh; } |
    shell bash -s
}

# forward makes each port on the VM's addresses reach the same port on this
# machine's loopback.
forward() { # port...
  shell sh -c "command -v socat >/dev/null || dnf install -y -q socat"
  units=""
  for port in "$@"; do
    units="$units lazycloud-forward-$port.service"
    printf '[Unit]\nDescription=Forward port %s to the developer machine\nAfter=network-online.target\n\n[Service]\nExecStart=/usr/bin/socat TCP-LISTEN:%s,fork,reuseaddr TCP:%s:%s\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n' \
      "$port" "$port" "$host_ip" "$port" | shell tee "/etc/systemd/system/lazycloud-forward-$port.service" >/dev/null
  done
  # shellcheck disable=SC2086 # unit names are separate words
  shell systemctl daemon-reload && shell systemctl enable --now $units
}

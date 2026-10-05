#!/bin/sh
# The local stack's host: a Lima VM from Amazon Linux 2023 set up by the node
# image recipe, so workloads run under the same Docker, containerd, gVisor
# and lazycloud-snapshotter as fleet hosts, and this machine's own Docker is
# never reconfigured. The server, scheduler, Postgres, Garage and the
# registry stay on this machine; the VM reaches them through forwards.
#
# Usage: deploy/local/host-vm.sh up|down|reset
#   up     download Lima and the image (pinned, checksums verified) into
#          .lazycloud/, create or start the VM, set it up once, and install
#          and start the agent from the local server (run.sh start runs it)
#   down   stop the VM
#   reset  delete the VM; the next up builds a fresh host
#
# It needs KVM access (the kvm group) and qemu-system-x86_64, not root.
# LAZYCLOUD_LIMA_HOME moves Lima's state, whose socket paths must stay short.
set -eu
cd "$(dirname "$0")/../.."
state=$PWD/.lazycloud

LIMA_VERSION=2.2.1
LIMA_SHA256=b391ac7fcac2b2a5a1628107756463188ded4d18d3104a40a66a8bb6fec01bcb
AL2023_RELEASE=2023.12.20260930.0
AL2023_IMAGE=al2023-kvm-$AL2023_RELEASE-kernel-6.1-x86_64.xfs.gpt.qcow2
AL2023_SHA256=aceaf11d27b8265a9aa681448b1e0223db4e3bfcfd13abde40d6654533e5718f

vm=lazycloud-host
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

exists() { "$limactl" list --quiet 2>/dev/null | grep -qx "$vm"; }

create() {
  mkdir -p "$state/vm" "$LIMA_HOME"
  fetch "$state/vm/$AL2023_IMAGE" "https://cdn.amazonlinux.com/al2023/os-images/$AL2023_RELEASE/kvm/$AL2023_IMAGE" "$AL2023_SHA256"
  cat >"$state/vm/$vm.yaml" <<YAML
vmType: qemu
arch: x86_64
cpus: ${LAZYCLOUD_VM_CPUS:-4}
memory: ${LAZYCLOUD_VM_MEMORY:-8GiB}
disk: ${LAZYCLOUD_VM_DISK:-60GiB}
images:
  - location: "$state/vm/$AL2023_IMAGE"
    arch: x86_64
mounts: []
containerd:
  system: false
  user: false
YAML
  "$limactl" create --tty=false --name "$vm" "$state/vm/$vm.yaml"
}

shell() { "$limactl" shell --workdir / "$vm" sudo "$@"; }

# setup runs the node image recipe once, then forwards the ports of the
# stack run.sh configures: the gateway (install script and agent releases),
# the agent's gRPC endpoint, the registry and Garage.
setup() {
  : "${LAZYCLOUD_IMAGE_REGISTRY:?run by deploy/local/run.sh}" "${LAZYCLOUD_OBJECT_STORE_ENDPOINT:?}" "${LAZYCLOUD_DOCKER_BRIDGE_IP:?}"
  store=${LAZYCLOUD_OBJECT_STORE_ENDPOINT##*:}
  forwards="${LAZYCLOUD_HTTP_ADDR##*:} ${LAZYCLOUD_GRPC_ADDR##*:} ${LAZYCLOUD_IMAGE_REGISTRY##*:} ${store%/}"
  if ! shell test -f /etc/lazycloud-node-image.json; then
    { echo "#!/bin/bash"; sed '/^#/d' deploy/host-pins.sh; echo VARIANT=cpu; sed 1d deploy/ami/node-setup.sh; } |
      shell bash -s
  fi
  bridge=$LAZYCLOUD_DOCKER_BRIDGE_IP
  units=""
  for port in $forwards; do
    units="$units lazycloud-forward-$port.service"
    printf '[Unit]\nDescription=Forward port %s to the developer machine\nAfter=network-online.target\n\n[Service]\nExecStart=/usr/bin/socat TCP-LISTEN:%s,fork,reuseaddr TCP:%s:%s\nRestart=always\n\n[Install]\nWantedBy=multi-user.target\n' \
      "$port" "$port" "$host_ip" "$port" | shell tee "/etc/systemd/system/lazycloud-forward-$port.service" >/dev/null
  done
  shell sh -c "command -v socat >/dev/null || dnf install -y -q socat"
  # Presigned URLs name the developer machine's Docker bridge address;
  # made the VM's own, the Garage forward answers it.
  printf '[Unit]\nDescription=Answer the developer machine'"'"'s Docker bridge address\nAfter=network-online.target\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/sh -c "ip -4 addr show | grep -q \\" %s/\\" || ip addr add %s/32 dev lo"\n\n[Install]\nWantedBy=multi-user.target\n' \
    "$bridge" "$bridge" | shell tee /etc/systemd/system/lazycloud-bridge-address.service >/dev/null
  # Builds push to the registry forward on the VM's loopback.
  printf '[Service]\nEnvironment=LAZYCLOUD_BUILD_NETWORK=host\n' |
    shell sh -c 'mkdir -p /etc/systemd/system/lazycloud-agent.service.d && cat >/etc/systemd/system/lazycloud-agent.service.d/local-host.conf'
  # shellcheck disable=SC2086 # unit names are separate words
  shell systemctl daemon-reload && shell systemctl enable --now lazycloud-bridge-address.service $units
}

# install_agent joins the VM the way hosts join, installing the release the
# local server publishes. A joined VM restarts its agent, which then
# updates itself to the server's current release.
install_agent() {
  if shell test -s /var/lib/lazycloud/agent/identity.json; then
    shell systemctl restart lazycloud-agent
    return
  fi
  gateway="http://127.0.0.1:${LAZYCLOUD_HTTP_ADDR##*:}"
  token=$(bin/server admin create-join-token)
  shell sh -c "curl -fsSL $gateway/install/agent | sh -s -- --gateway $gateway --server 127.0.0.1:${LAZYCLOUD_GRPC_ADDR##*:} --server-plaintext --background --join-token $token"
}

up() {
  install_lima
  exists || create
  [ "$("$limactl" list --format '{{.Status}}' "$vm")" = Running ] || "$limactl" start --tty=false "$vm"
  setup
  install_agent
}

case "${1:-}" in
  up) up ;;
  down) [ -x "$limactl" ] && exists && "$limactl" stop "$vm" || true ;;
  reset) [ -x "$limactl" ] && exists && "$limactl" delete --force "$vm" || true ;;
  *) echo "usage: $0 up|down|reset" >&2; exit 2 ;;
esac

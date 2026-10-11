#!/bin/sh
# The local stack's host: a Lima VM from Amazon Linux 2023 set up by the node
# image recipe, so workloads run under the same Docker, containerd, gVisor
# and lazycloud-snapshotter as fleet hosts, and this machine's own Docker is
# never reconfigured. The server, scheduler, Postgres, Garage and the
# registry stay on this machine; the VM reaches them through forwards.
#
# Usage: deploy/local/host-vm.sh up|down|reset
#   up     download Lima and the image (pinned, checksums verified) into
#          .lazycloud/, create or start the VM, set it up once, replace its
#          snapshotter when the release's differs and no container runs, and
#          install and start the agent from the local server (run.sh start
#          runs it)
#   down   stop the VM
#   reset  delete the VM; the next up builds a fresh host
#
# lima.sh says what the VM needs from this machine.
set -eu
cd "$(dirname "$0")/../.."
state=$PWD/.lazycloud
vm=lazycloud-host
# shellcheck source=deploy/local/lima.sh
. deploy/local/lima.sh

# setup runs the node image recipe once, then forwards the ports of the
# stack run.sh configures: the gateway (install script and agent releases),
# the agent's gRPC endpoint, the registry and Garage.
setup() {
  : "${LAZYCLOUD_IMAGE_REGISTRY:?run by deploy/local/run.sh}" "${LAZYCLOUD_OBJECT_STORE_ENDPOINT:?}" "${LAZYCLOUD_DOCKER_BRIDGE_IP:?}"
  store=${LAZYCLOUD_OBJECT_STORE_ENDPOINT##*:}
  forwards="${LAZYCLOUD_HTTP_ADDR##*:} ${LAZYCLOUD_GRPC_ADDR##*:} ${LAZYCLOUD_IMAGE_REGISTRY##*:} ${store%/}"
  node_image
  # shellcheck disable=SC2086 # the ports are separate words
  forward $forwards
  bridge=$LAZYCLOUD_DOCKER_BRIDGE_IP
  # Presigned URLs name the developer machine's Docker bridge address;
  # made the VM's own, the Garage forward answers it.
  printf '[Unit]\nDescription=Answer the developer machine'"'"'s Docker bridge address\nAfter=network-online.target\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/bin/sh -c "ip -4 addr show | grep -q \\" %s/\\" || ip addr add %s/32 dev lo"\n\n[Install]\nWantedBy=multi-user.target\n' \
    "$bridge" "$bridge" | shell tee /etc/systemd/system/lazycloud-bridge-address.service >/dev/null
  # Builds push to the registry forward on the VM's loopback.
  printf '[Service]\nEnvironment=LAZYCLOUD_BUILD_NETWORK=host\n' |
    shell sh -c 'mkdir -p /etc/systemd/system/lazycloud-agent.service.d && cat >/etc/systemd/system/lazycloud-agent.service.d/local-host.conf'
  shell systemctl daemon-reload && shell systemctl enable --now lazycloud-bridge-address.service
}

# refresh_snapshotter replaces the VM's snapshotter with the one in the
# agent release when they differ. Agent updates never replace a running
# snapshotter, since its FUSE mounts die with it, so this restarts it only
# while no container runs and otherwise says how to start over.
refresh_snapshotter() {
  : "${LAZYCLOUD_AGENT_ARCHIVE:?run by deploy/local/run.sh}"
  binary=/usr/local/lib/lazycloud/lazycloud-snapshotter
  shell test -x "$binary" || return 0
  want=$(tar -xzOf "$LAZYCLOUD_AGENT_ARCHIVE" lazycloud-snapshotter | sha256sum | cut -d' ' -f1)
  [ "$(shell sha256sum "$binary" | cut -d' ' -f1)" = "$want" ] && return 0
  # A stopped agent starts no container while this checks.
  shell systemctl stop lazycloud-agent
  if [ -n "$(shell docker ps --quiet)" ]; then
    echo "the VM's lazycloud-snapshotter differs from this tree's, and containers run there; deploy/local/host-vm.sh reset gives a fresh host" >&2
    return 0
  fi
  tar -xzOf "$LAZYCLOUD_AGENT_ARCHIVE" lazycloud-snapshotter |
    shell sh -c "cat >$binary.new && chmod 0755 $binary.new && mv $binary.new $binary"
  shell systemctl restart lazycloud-snapshotter
  echo "restarted the VM's lazycloud-snapshotter on this tree's build" >&2
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
  start 4 8GiB 'mounts: []'
  setup
  refresh_snapshotter
  install_agent
}

case "${1:-}" in
  up) up ;;
  down) stop_vm ;;
  reset) delete_vm ;;
  *) echo "usage: $0 up|down|reset" >&2; exit 2 ;;
esac

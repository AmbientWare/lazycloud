#!/bin/sh
# Runs the tests CI runs on runners set up as hosts (the agent owner tests,
# the disk engine's root tests and acceptance) in a Lima VM built like the
# local stack's host (lima.sh), with the Go toolchain go.mod pins and the
# snapshotter installed by the script CI runs. The VM mounts this machine's
# main checkout, worktrees included, read-write at the same path, and
# reaches the compose test stack on this machine through forwards, so
# `docker compose -f compose.test.yaml up -d --wait` serves it too.
#
# Usage: deploy/local/test-vm.sh up|test|acceptance|down|reset
#   up                download Lima and the image into the main checkout's
#                     .lazycloud/, create or start the VM, set it up once,
#                     and install this tree's snapshotter while no
#                     container runs
#   test ARGS         go test ARGS as root in this tree
#   acceptance ARGS   acceptance.sh prepare here, then acceptance.sh run
#                     ARGS as root in the VM
#   down              stop the VM
#   reset             delete the VM
#
# test and acceptance pass on this shell's LAZYCLOUD_TEST_* settings and
# exit with go test's status.
set -eu
cd "$(dirname "$0")/../.."
tree=$PWD
checkout=$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")
case $tree in
  "$checkout" | "$checkout"/*) ;;
  *) echo "the test VM mounts $checkout, which does not hold $tree" >&2; exit 1 ;;
esac
state=$checkout/.lazycloud
vm=lazycloud-test
# shellcheck source=deploy/local/lima.sh
. deploy/local/lima.sh

# The image's kernel has no 9p, so the checkout is shared over virtiofs.
# Lima finds virtiofsd through the vhost-user directory beside the QEMU it
# runs and starts it without a sandbox option; the namespace sandbox it
# then defaults to needs user namespaces this machine may refuse, so a
# wrapper runs it without one.
VIRTIOFSD_VERSION=1.14.0
VIRTIOFSD_SHA256=2e4fe9571f492b00baa34bc4e708e950039c5da05b830b31a8d179cb6ac8978e
virtiofsd=$state/virtiofsd-$VIRTIOFSD_VERSION
export QEMU_SYSTEM_X86_64="$virtiofsd/bin/qemu-system-x86_64"

install_virtiofsd() {
  [ -x "$virtiofsd/bin/virtiofsd" ] && return 0
  fetch "$state/virtiofsd.zip" "https://gitlab.com/-/project/21523468/uploads/f505704014ae7a816e515f2a05a93d8b/virtiofsd-v$VIRTIOFSD_VERSION.zip" "$VIRTIOFSD_SHA256"
  mkdir -p "$virtiofsd/bin" "$virtiofsd/share/qemu/vhost-user"
  unzip -p "$state/virtiofsd.zip" target/x86_64-unknown-linux-musl/release/virtiofsd >"$virtiofsd/virtiofsd"
  chmod 0755 "$virtiofsd/virtiofsd"
  rm "$state/virtiofsd.zip"
  ln -sf "$(command -v qemu-system-x86_64)" "$QEMU_SYSTEM_X86_64"
  printf '{"type": "fs", "binary": "%s"}\n' "$virtiofsd/bin/virtiofsd" >"$virtiofsd/share/qemu/vhost-user/50-virtiofsd.json"
  printf '#!/bin/sh\nexec "%s" --sandbox none "$@"\n' "$virtiofsd/virtiofsd" >"$virtiofsd/bin/virtiofsd"
  chmod 0755 "$virtiofsd/bin/virtiofsd"
}

# mount_checkout mounts the checkout. The image's cloud-init skips mounts
# named by tag, as Lima names them, so this adds Lima's to fstab once.
mount_checkout() {
  shell mountpoint -q "$checkout" && return 0
  shell sh -c "grep -q ' virtiofs ' /etc/fstab ||
    sed -n 's/^- \[\(lima-[^,]*\), \([^,]*\), virtiofs, \"\([^\"]*\)\".*/\1 \2 virtiofs \3 0 0/p' /mnt/lima-cidata/user-data >>/etc/fstab"
  shell mkdir -p "$checkout"
  shell mount "$checkout"
}

setup() {
  mount_checkout
  node_image
  # The node recipe removes the compiler, which -race needs;
  # install-snapshotter.sh writes a polkit rule.
  shell sh -c "command -v gcc >/dev/null && test -d /etc/polkit-1/rules.d || dnf install -y -q gcc polkit"
  go=$(sed -n 's/^toolchain //p' go.mod)
  if [ "$(shell sh -c 'head -1 /usr/local/go/VERSION 2>/dev/null' || true)" != "$go" ]; then
    archive=$go.linux-amd64.tar.gz
    shell sh -c "cd /tmp && curl -fsSLO https://dl.google.com/go/$archive &&
      echo \"\$(curl -fsSL https://dl.google.com/go/$archive.sha256)  $archive\" | sha256sum -c - &&
      rm -rf /usr/local/go && tar -xzf $archive -C /usr/local && rm $archive"
  fi
  # shellcheck disable=SC2046 # one word per port
  forward $(sed -n 's/.*"127\.0\.0\.1:\([0-9]*\):.*/\1/p' compose.test.yaml)
  # A running snapshotter keeps its version, so this tree's replaces it
  # only on an idle VM.
  if [ -z "$(shell docker ps --quiet)" ]; then shell systemctl stop lazycloud-snapshotter 2>/dev/null || true; fi
  run deploy/local/install-snapshotter.sh
}

# run runs a command as root in this tree with the tools hosts and CI
# runners have on PATH. Binaries the tests build skip VCS stamping, which
# would read the checkout's git index across the mount.
run() {
  for name in $(env | sed -n 's/^\(LAZYCLOUD_TEST_[A-Z0-9_]*\)=.*/\1/p'); do
    eval "set -- \"\$name=\${$name}\" \"\$@\""
  done
  "$limactl" shell --workdir "$tree" "$vm" sudo env \
    PATH=/usr/local/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin GOFLAGS=-buildvcs=false "$@"
}

ready() {
  [ "$("$limactl" list --format '{{.Status}}' "$vm" 2>/dev/null)" = Running ] && return 0
  echo "the test VM is not running; deploy/local/test-vm.sh up starts it" >&2
  exit 1
}

case "${1:-}" in
  up)
    install_virtiofsd
    start 16 24GiB "mounts:
  - location: \"$checkout\"
    writable: true
mountType: virtiofs"
    setup
    ;;
  test)
    shift
    ready
    run go test "$@"
    ;;
  acceptance)
    shift
    ready
    deploy/local/acceptance.sh prepare
    run deploy/local/acceptance.sh run "$@"
    ;;
  down) stop_vm ;;
  reset) delete_vm ;;
  *) echo "usage: $0 up|test|acceptance|down|reset" >&2; exit 2 ;;
esac

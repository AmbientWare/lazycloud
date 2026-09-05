#!/bin/bash
set -Eeuo pipefail

report_failure() {
  bake_status=$?
  trap - ERR
  systemctl --no-pager --full status systemd-zram-setup@zram0.service dev-zram0.swap || true
  journalctl -b --no-pager -n 60 -u systemd-zram-setup@zram0.service || true
  modinfo zram || true
  exit "$bake_status"
}
trap report_failure ERR

. /etc/os-release
test "$ID" = ubuntu
test "$VERSION_ID" = 24.04
test "$(uname -m)" = x86_64
test -e /dev/net/tun

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  fuse3 systemd-zram-generator "linux-modules-extra-$(uname -r)"
modinfo zram

install -d /etc/systemd
printf '[zram0]\nzram-size = ram / 4\nswap-priority = 100\nhost-memory-limit = none\n' \
  > /etc/systemd/zram-generator.conf
printf 'vm.swappiness = 180\nvm.page-cluster = 0\n' \
  > /etc/sysctl.d/60-lazycloud-zram.conf
systemctl daemon-reload
systemctl start dev-zram0.swap
sysctl --system
systemctl enable --now docker
docker info --format '{{.CgroupVersion}}' | grep -x 2
wg --version
iptables --version
swapon --show
grep -q '^/dev/zram0 ' /proc/swaps

# The snapshot must contain no join credential or reusable SSH identity.
test ! -d /var/lib/lazycloud/agent
truncate -s 0 /root/.ssh/authorized_keys
rm -f /etc/ssh/ssh_host_rsa_key /etc/ssh/ssh_host_rsa_key.pub \
  /etc/ssh/ssh_host_ecdsa_key /etc/ssh/ssh_host_ecdsa_key.pub \
  /etc/ssh/ssh_host_ed25519_key /etc/ssh/ssh_host_ed25519_key.pub
cloud-init clean --logs --machine-id --seed --configs all
apt-get clean
sync

#!/usr/bin/env bash
set -euo pipefail

# Build Talos OS snapshot on Hetzner Cloud via Packer.
# Downloads a factory image with gVisor extension baked in,
# then creates a snapshot using Packer.
#
# Prerequisites:
#   - HCLOUD_TOKEN set in environment
#   - hcloud, packer installed
#
# Usage:
#   export HCLOUD_TOKEN=...
#   ./infrastructure/build-snapshot.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PACKER_DIR="$SCRIPT_DIR/packer"

if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
  echo "HCLOUD_TOKEN not set."
  read -rsp "Enter Hetzner Cloud token: " HCLOUD_TOKEN
  echo
  if [[ -z "$HCLOUD_TOKEN" ]]; then
    echo "ERROR: Token cannot be empty" >&2
    exit 1
  fi
  export HCLOUD_TOKEN
fi

# Delete existing Talos snapshots
echo "==> Checking for existing Talos snapshots..."
EXISTING=$(hcloud image list -t snapshot -l os=talos -o noheader -o columns=id 2>/dev/null || true)
if [[ -n "$EXISTING" ]]; then
  for id in $EXISTING; do
    echo "    Deleting snapshot $id"
    hcloud image delete "$id"
  done
else
  echo "    No existing snapshots found"
fi

# Build new snapshot
echo "==> Building Talos snapshot (gVisor)..."
cd "$PACKER_DIR"
packer init .
packer build -var "server_location=ash" talos.pkr.hcl

echo ""
echo "========================================="
echo "  Snapshot built successfully!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  cd infrastructure/terraform"
echo "  terraform init"
echo "  terraform apply -var=\"hcloud_token=\$HCLOUD_TOKEN\""
echo "  terraform output --raw kubeconfig > ./kubeconfig"
echo "  terraform output --raw talosconfig > ./talosconfig"
echo "  chmod 600 ./kubeconfig ./talosconfig"
echo "  export KUBECONFIG=\$(pwd)/kubeconfig"
echo "  source ./infrastructure/kubesetup.sh"

#!/usr/bin/env bash
# Merges kubeconfig from Terraform into ~/.kube/config and sets active context.
# Exports talosconfig.
# Usage: source ./infrastructure/kubesetup.sh [cluster-id]
# Example: source ./infrastructure/kubesetup.sh ash-1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_ID="${1:-ash-1}"
TF_DIR="$SCRIPT_DIR/terraform/clusters/$CLUSTER_ID"

if [ ! -d "$TF_DIR" ]; then
  echo "Error: Cluster directory not found: $TF_DIR"
  return 1
fi

# Extract configs from Terraform state
terraform -chdir="$TF_DIR" output -raw kubeconfig > "$SCRIPT_DIR/terraform/kubeconfig" 2>/dev/null
terraform -chdir="$TF_DIR" output -raw talosconfig > "$SCRIPT_DIR/terraform/talosconfig" 2>/dev/null
chmod 600 "$SCRIPT_DIR/terraform/kubeconfig" "$SCRIPT_DIR/terraform/talosconfig"

# Get the context name from the extracted kubeconfig
KUBECONFIG_FILE="$SCRIPT_DIR/terraform/kubeconfig"
TALOSCONFIG_FILE="$SCRIPT_DIR/terraform/talosconfig"
CONTEXT_NAME=$(kubectl config get-contexts --kubeconfig="$KUBECONFIG_FILE" -o name 2>/dev/null | head -1)

# Merge into default kubeconfig (overwrites existing cluster/user/context with same name)
mkdir -p ~/.kube
KUBECONFIG="$KUBECONFIG_FILE:${HOME}/.kube/config" kubectl config view --flatten > "${HOME}/.kube/config.merged"
mv "${HOME}/.kube/config.merged" "${HOME}/.kube/config"
chmod 600 "${HOME}/.kube/config"

# Activate the cluster context
if [ -n "$CONTEXT_NAME" ]; then
  kubectl config use-context "$CONTEXT_NAME"
fi

# Export talosconfig
export TALOSCONFIG="$TALOSCONFIG_FILE"

echo "Merged kubeconfig into ~/.kube/config (context: $CONTEXT_NAME)"
echo "TALOSCONFIG=$TALOSCONFIG"

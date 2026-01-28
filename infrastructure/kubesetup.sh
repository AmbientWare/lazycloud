#!/usr/bin/env bash
# Merges kubeconfig from Terraform into ~/.kube/config and sets active context.
# Exports talosconfig.
# Usage: source ./infrastructure/kubesetup.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$SCRIPT_DIR/terraform"

# Extract configs from Terraform state
terraform -chdir="$TF_DIR" output --raw kubeconfig > "$TF_DIR/kubeconfig"
terraform -chdir="$TF_DIR" output --raw talosconfig > "$TF_DIR/talosconfig"
chmod 600 "$TF_DIR/kubeconfig" "$TF_DIR/talosconfig"

# Get the context name from the extracted kubeconfig
CONTEXT_NAME=$(kubectl config get-contexts --kubeconfig="$TF_DIR/kubeconfig" -o name 2>/dev/null | head -1)

# Merge into default kubeconfig (overwrites existing cluster/user/context with same name)
mkdir -p ~/.kube
KUBECONFIG="$TF_DIR/kubeconfig:${HOME}/.kube/config" kubectl config view --flatten > "${HOME}/.kube/config.merged"
mv "${HOME}/.kube/config.merged" "${HOME}/.kube/config"
chmod 600 "${HOME}/.kube/config"

# Activate the cluster context
if [ -n "$CONTEXT_NAME" ]; then
  kubectl config use-context "$CONTEXT_NAME"
fi

# Export talosconfig
export TALOSCONFIG="$TF_DIR/talosconfig"

echo "Merged kubeconfig into ~/.kube/config (context: $CONTEXT_NAME)"
echo "TALOSCONFIG=$TALOSCONFIG"

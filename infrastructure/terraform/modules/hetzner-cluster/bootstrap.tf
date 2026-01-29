# -----------------------------------------------------------------------------
# Cluster bootstrap: CNI, CSI, storage, gVisor, secrets, ArgoCD
#
# Order of operations is controlled via depends_on chains:
#   0. Wait for cluster API to be healthy (talos_cluster_health)
#   1. Cilium CNI (nodes become Ready)
#   2. JuiceFS CSI driver + secret
#   3. Storage classes
#   4. gVisor RuntimeClass
#   5. Namespaces + secrets (hcloud token, AWS creds, app namespaces, monitoring)
#   6. ArgoCD (+ repo credential)
#
# NOTE: External Secrets Operator is managed by ArgoCD (not Terraform).
#       Terraform only creates the namespace + aws-sm-credentials secret for bootstrap.
#
# DESTROY: Set var.skip_bootstrap = true, run `terraform apply`, then `terraform destroy`
# -----------------------------------------------------------------------------

locals {
  deploy_dir = "${path.module}/../../../../deploy"

  # Dynamic autoscaler node config key based on worker type and location
  autoscaler_node_config_key = "${var.worker_type}-worker-${var.location}"

  # Bootstrap control - set to 0 to remove all K8s resources before destroy
  bootstrap_count = var.skip_bootstrap ? 0 : 1

  # For for_each resources, use empty map when skipping
  app_namespaces = var.skip_bootstrap ? {} : { for ns in var.app_namespaces : ns.name => ns }
}

# =============================================================================
# 1. Cilium CNI — must be first, nodes are NotReady without it
# =============================================================================

resource "helm_release" "cilium" {
  count = local.bootstrap_count

  name       = "cilium"
  namespace  = "kube-system"
  repository = "https://helm.cilium.io/"
  chart      = "cilium"
  version    = "1.16.5"

  values = [file("${local.deploy_dir}/platform/cilium/values.yaml")]

  wait    = true
  timeout = 300

  # Wait for cluster API to be healthy before installing CNI
  depends_on = [null_resource.wait_for_cluster_health]
}

# =============================================================================
# 1b. Kubelet CSR Approver — auto-approves kubelet serving certificate CSRs
# =============================================================================

resource "helm_release" "kubelet_csr_approver" {
  count = local.bootstrap_count

  name       = "kubelet-csr-approver"
  namespace  = "kube-system"
  repository = "https://postfinance.github.io/kubelet-csr-approver"
  chart      = "kubelet-csr-approver"
  version    = "1.2.2"

  values = [yamlencode({
    # Only approve CSRs for nodes in our cluster
    providerRegex = "^${var.cluster_name}-.*$"
    # Maximum expiration time for certificates (365 days)
    maxExpirationSeconds = 31536000
    # Allow bypass for DNS SANs (Talos nodes may request with hostname)
    bypassDnsResolution = true
  })]

  wait    = true
  timeout = 120

  depends_on = [helm_release.cilium]
}

# =============================================================================
# 2. JuiceFS CSI driver + credentials
# =============================================================================

resource "helm_release" "juicefs_csi" {
  count = local.bootstrap_count

  name       = "juicefs-csi-driver"
  namespace  = "kube-system"
  repository = "https://juicedata.github.io/charts/"
  chart      = "juicefs-csi-driver"

  values = [yamlencode({
    storageClasses = [{ enabled = false }]
    # Talos Linux has a read-only /etc — this skips the hostPath
    # /etc/updatedb.conf volume that mount pods create by default
    immutable = true
  })]

  wait    = true
  timeout = 300

  depends_on = [helm_release.cilium]
}

resource "kubernetes_secret" "juicefs" {
  count = local.bootstrap_count

  metadata {
    name      = "juicefs-secret"
    namespace = "kube-system"
  }

  data = {
    name         = var.juicefs_name
    token        = var.juicefs_token
    "access-key" = var.aws_access_key_id
    "secret-key" = var.aws_secret_access_key
  }

  depends_on = [helm_release.cilium]
}

# =============================================================================
# 3. Storage classes (local chart)
# =============================================================================

resource "helm_release" "storage_classes" {
  count = local.bootstrap_count

  name      = "storage-classes"
  namespace = "kube-system"
  chart     = "${local.deploy_dir}/platform/storage-classes"

  depends_on = [helm_release.juicefs_csi]
}

# =============================================================================
# 4. gVisor RuntimeClass (local chart)
# =============================================================================

resource "helm_release" "gvisor" {
  count = local.bootstrap_count

  name      = "gvisor"
  namespace = "kube-system"
  chart     = "${local.deploy_dir}/platform/gvisor"

  # Disable placeholder pods during bootstrap — the runsc handler may not be
  # fully ready when Cilium just came up, causing the placeholder to hang.
  # ArgoCD will manage the full chart (with placeholders) once stable.
  values = [yamlencode({
    placeholder = { enabled = false }
  })]

  depends_on = [helm_release.cilium]
}

# =============================================================================
# 5. Namespaces + secrets
# =============================================================================

# Hetzner Cloud token (for cluster-autoscaler)
resource "kubernetes_secret" "hcloud_token" {
  count = local.bootstrap_count

  metadata {
    name      = "hetzner-cloud-token"
    namespace = "kube-system"
  }

  data = {
    token = var.hcloud_token
  }

  depends_on = [helm_release.cilium]
}

# Cluster autoscaler config (Talos machine config as cloud-init for new workers)
resource "kubernetes_secret" "cluster_autoscaler_config" {
  count = local.bootstrap_count

  metadata {
    name      = "cluster-autoscaler-config"
    namespace = "kube-system"
  }

  data = {
    cluster-config = base64encode(jsonencode({
      imagesForArch = {
        amd64 = tostring(data.hcloud_image.talos.id)
      }
      nodeConfigs = {
        (local.autoscaler_node_config_key) = {
          cloudInit = data.talos_machine_configuration.worker[0].machine_configuration
          labels = {
            "runtime"  = "gvisor"
            "workload" = "sandbox"
          }
        }
      }
    }))
  }

  depends_on = [helm_release.cilium]
}

# AWS credentials for External Secrets Operator
resource "kubernetes_namespace" "external_secrets" {
  count = local.bootstrap_count

  metadata {
    name = "external-secrets-system"
  }

  depends_on = [helm_release.cilium]
}

resource "kubernetes_secret" "aws_sm_credentials" {
  count = local.bootstrap_count

  metadata {
    name      = "aws-sm-credentials"
    namespace = "external-secrets-system"
  }

  data = {
    "access-key" = var.aws_access_key_id
    "secret-key" = var.aws_secret_access_key
  }

  depends_on = [kubernetes_namespace.external_secrets]
}

# App namespaces (configurable via variable)
resource "kubernetes_namespace" "app" {
  for_each = local.app_namespaces

  metadata {
    name   = each.value.name
    labels = each.value.labels
  }

  depends_on = [helm_release.cilium]
}

# Monitoring namespace — node-exporter needs privileged PodSecurity
resource "kubernetes_namespace" "monitoring" {
  count = local.bootstrap_count

  metadata {
    name = "monitoring"
    labels = {
      "pod-security.kubernetes.io/enforce" = "privileged"
    }
  }

  depends_on = [helm_release.cilium]
}

# Cleanup for monitoring namespace on destroy
resource "null_resource" "monitoring_cleanup" {
  count = local.bootstrap_count

  triggers = {
    namespace = try(kubernetes_namespace.monitoring[0].metadata[0].name, "")
  }

  provisioner "local-exec" {
    when        = destroy
    command     = <<-EOT
      echo "Cleaning up monitoring namespace..."
      # Force delete stuck pods
      kubectl delete pods -n monitoring --all --force --grace-period=0 2>/dev/null || true
      # Remove PVC finalizers
      for pvc in $(kubectl get pvc -n monitoring -o name 2>/dev/null); do
        kubectl patch "$pvc" -n monitoring --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]' 2>/dev/null || true
      done
      echo "Monitoring cleanup complete"
    EOT
    interpreter = ["bash", "-c"]
  }

  # Depends on monitoring namespace - on destroy, this runs FIRST
  depends_on = [kubernetes_namespace.monitoring]
}

# =============================================================================
# 6. ArgoCD
# =============================================================================

resource "kubernetes_namespace" "argocd" {
  count = local.bootstrap_count

  metadata {
    name = "argocd"
  }

  depends_on = [helm_release.cilium]
}

# ArgoCD cluster-local secret (so ArgoCD can deploy to itself)
resource "kubernetes_secret" "argocd_cluster_local" {
  count = local.bootstrap_count

  metadata {
    name      = "cluster-local"
    namespace = "argocd"
    labels = {
      "argocd.argoproj.io/secret-type" = "cluster"
      "provider"                       = "hetzner"
      "cluster_id"                     = var.cluster_id
    }
  }

  data = {
    name   = "local"
    server = "https://kubernetes.default.svc"
  }

  depends_on = [kubernetes_namespace.argocd]
}

resource "helm_release" "argocd" {
  count = local.bootstrap_count

  name       = "argocd"
  namespace  = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"

  values = [yamlencode({
    global = {
      domain = var.argocd_domain
    }
    server = {
      ingress = {
        enabled          = true
        ingressClassName = "nginx"
        annotations = {
          "nginx.ingress.kubernetes.io/ssl-passthrough"  = "true"
          "nginx.ingress.kubernetes.io/backend-protocol" = "HTTPS"
        }
      }
    }
  })]

  wait    = true
  timeout = 600

  depends_on = [kubernetes_namespace.argocd, kubernetes_secret.argocd_cluster_local]
}

# ArgoCD repo credentials
resource "kubernetes_secret" "argocd_repo" {
  count = local.bootstrap_count

  metadata {
    name      = "lazycloud-repo"
    namespace = "argocd"
    labels = {
      "argocd.argoproj.io/secret-type" = "repository"
    }
  }

  data = {
    type          = "git"
    url           = var.git_repo_url
    sshPrivateKey = file(pathexpand(var.argocd_repo_ssh_key_path))
  }

  depends_on = [helm_release.argocd]
}

# =============================================================================
# Cleanup on destroy - removes finalizers so skip_bootstrap=true works cleanly
# =============================================================================
# This resource depends on argocd_repo (end of the chain), so on destroy it runs FIRST.
# Destroy order: cleanup -> repo -> helm -> secrets -> namespaces

resource "null_resource" "bootstrap_cleanup" {
  count = local.bootstrap_count

  triggers = {
    # Recreate if these change
    argocd_ns     = try(kubernetes_namespace.argocd[0].metadata[0].name, "")
    monitoring_ns = try(kubernetes_namespace.monitoring[0].metadata[0].name, "")
  }

  provisioner "local-exec" {
    when        = destroy
    command     = <<-EOT
      echo "=== Bootstrap Cleanup ==="

      # Remove ArgoCD application/applicationset finalizers and delete
      echo "Cleaning up ArgoCD resources..."
      for app in $(kubectl get applications.argoproj.io -n argocd -o name 2>/dev/null); do
        kubectl patch "$app" -n argocd --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]' 2>/dev/null || true
      done
      for appset in $(kubectl get applicationsets.argoproj.io -n argocd -o name 2>/dev/null); do
        kubectl patch "$appset" -n argocd --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]' 2>/dev/null || true
      done
      kubectl delete applications.argoproj.io -n argocd --all --timeout=10s 2>/dev/null || true
      kubectl delete applicationsets.argoproj.io -n argocd --all --timeout=10s 2>/dev/null || true

      # Clean up External Secrets resources (CRs block namespace deletion)
      echo "Cleaning up External Secrets resources..."
      for es in $(kubectl get externalsecrets -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\n"}{end}' 2>/dev/null); do
        ns=$(echo "$es" | cut -d/ -f1)
        name=$(echo "$es" | cut -d/ -f2)
        kubectl patch externalsecret "$name" -n "$ns" --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]' 2>/dev/null || true
      done
      kubectl delete externalsecrets --all -A --force --grace-period=0 2>/dev/null || true
      kubectl delete clustersecretstores --all --force --grace-period=0 2>/dev/null || true

      # Force delete all helm releases that might be stuck
      echo "Cleaning up Helm releases..."
      for ns in external-secrets-system argocd monitoring ingress-nginx cloudflare-system; do
        for release in $(helm list -n "$ns" -q 2>/dev/null); do
          helm uninstall "$release" -n "$ns" --wait=false 2>/dev/null || true
        done
      done

      # Force delete stuck pods and PVCs in all app namespaces
      echo "Force deleting pods and PVCs..."
      for ns in monitoring argocd lazycloud-prod lazycloud-staging external-secrets-system ingress-nginx cloudflare-system; do
        kubectl delete pods -n "$ns" --all --force --grace-period=0 2>/dev/null || true
        for pvc in $(kubectl get pvc -n "$ns" -o name 2>/dev/null); do
          kubectl patch "$pvc" -n "$ns" --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]' 2>/dev/null || true
        done
        kubectl delete pvc -n "$ns" --all --force --grace-period=0 2>/dev/null || true
      done

      # Remove namespace finalizers so they can be deleted
      echo "Removing namespace finalizers..."
      for ns in monitoring argocd lazycloud-prod lazycloud-staging external-secrets-system ingress-nginx cloudflare-system argo-rollouts; do
        kubectl patch namespace "$ns" --type=json -p='[{"op": "remove", "path": "/metadata/finalizers"}]' 2>/dev/null || true
        kubectl patch namespace "$ns" --type=json -p='[{"op": "remove", "path": "/spec/finalizers"}]' 2>/dev/null || true
      done

      # Force finalize stuck namespaces via API (nuclear option for Terminating namespaces)
      echo "Force finalizing stuck namespaces..."
      for ns in monitoring argocd lazycloud-prod lazycloud-staging external-secrets-system ingress-nginx cloudflare-system argo-rollouts; do
        kubectl get namespace "$ns" -o json 2>/dev/null | \
          jq '.spec.finalizers = []' | \
          kubectl replace --raw "/api/v1/namespaces/$ns/finalize" -f - 2>/dev/null || true
      done

      # Wait for namespaces to actually be deleted (avoid race with Terraform)
      echo "Waiting for namespaces to be deleted..."
      NAMESPACES="monitoring argocd lazycloud-prod lazycloud-staging external-secrets-system ingress-nginx cloudflare-system argo-rollouts"
      TIMEOUT=180
      ELAPSED=0
      while [ $ELAPSED -lt $TIMEOUT ]; do
        REMAINING=""
        for ns in $NAMESPACES; do
          if kubectl get namespace "$ns" >/dev/null 2>&1; then
            REMAINING="$REMAINING $ns"
          fi
        done
        if [ -z "$REMAINING" ]; then
          echo "All namespaces deleted"
          break
        fi
        echo "Waiting for:$REMAINING ($ELAPSED/$TIMEOUT sec)"
        sleep 2
        ELAPSED=$((ELAPSED + 2))
      done

      echo "=== Cleanup complete ==="
    EOT
    interpreter = ["bash", "-c"]
  }

  # Depends on repo secret (end of chain) - on destroy, this runs FIRST
  depends_on = [kubernetes_secret.argocd_repo]
}

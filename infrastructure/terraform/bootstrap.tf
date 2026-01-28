# -----------------------------------------------------------------------------
# Cluster bootstrap: CNI, CSI, storage, gVisor, secrets, ArgoCD
#
# Order of operations is controlled via depends_on chains:
#   1. Cilium CNI (nodes become Ready)
#   2. JuiceFS CSI driver + secret
#   3. Storage classes
#   4. gVisor RuntimeClass
#   5. Namespaces + secrets (hcloud token, AWS creds, app namespaces, monitoring)
#   6. ArgoCD (+ repo credential)
# -----------------------------------------------------------------------------

locals {
  deploy_dir = "${path.module}/../../deploy"
}

# =============================================================================
# 1. Cilium CNI — must be first, nodes are NotReady without it
# =============================================================================

resource "helm_release" "cilium" {
  name       = "cilium"
  namespace  = "kube-system"
  repository = "https://helm.cilium.io/"
  chart      = "cilium"
  version    = "1.16.5"

  values = [file("${local.deploy_dir}/platform/cilium/values.yaml")]

  wait    = true
  timeout = 300

  depends_on = [talos_machine_bootstrap.this]
}

# =============================================================================
# 2. JuiceFS CSI driver + credentials
# =============================================================================

resource "helm_release" "juicefs_csi" {
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
  name      = "storage-classes"
  namespace = "kube-system"
  chart     = "${local.deploy_dir}/platform/storage-classes"

  depends_on = [helm_release.juicefs_csi]
}

# =============================================================================
# 4. gVisor RuntimeClass (local chart)
# =============================================================================

resource "helm_release" "gvisor" {
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
        "cpx41-worker-ash" = {
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
  metadata {
    name = "external-secrets-system"
  }

  depends_on = [helm_release.cilium]
}

resource "kubernetes_secret" "aws_sm_credentials" {
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

# App namespaces (external-secrets creates ExternalSecrets targeting these)
resource "kubernetes_namespace" "lazycloud_prod" {
  metadata {
    name = "lazycloud-prod"
  }

  depends_on = [helm_release.cilium]
}

resource "kubernetes_namespace" "lazycloud_staging" {
  metadata {
    name = "lazycloud-staging"
  }

  depends_on = [helm_release.cilium]
}

# Monitoring namespace — node-exporter needs privileged PodSecurity
resource "kubernetes_namespace" "monitoring" {
  metadata {
    name = "monitoring"
    labels = {
      "pod-security.kubernetes.io/enforce" = "privileged"
    }
  }

  depends_on = [helm_release.cilium]
}

# =============================================================================
# 6. ArgoCD
# =============================================================================

resource "kubernetes_namespace" "argocd" {
  metadata {
    name = "argocd"
  }

  depends_on = [helm_release.cilium]
}

# ArgoCD cluster-local secret (so ArgoCD can deploy to itself)
resource "kubernetes_secret" "argocd_cluster_local" {
  metadata {
    name      = "cluster-local"
    namespace = "argocd"
    labels = {
      "argocd.argoproj.io/secret-type" = "cluster"
      "provider"                       = "hetzner"
    }
  }

  data = {
    name   = "local"
    server = "https://kubernetes.default.svc"
  }

  depends_on = [kubernetes_namespace.argocd]
}

resource "helm_release" "argocd" {
  name       = "argocd"
  namespace  = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"

  values = [yamlencode({
    global = {
      domain = "argocd.lazycloud.dev"
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
  metadata {
    name      = "lazycloud-repo"
    namespace = "argocd"
    labels = {
      "argocd.argoproj.io/secret-type" = "repository"
    }
  }

  data = {
    type          = "git"
    url           = "git@github.com:AmbientWare/lazycloud.git"
    sshPrivateKey = file(pathexpand(var.argocd_repo_ssh_key_path))
  }

  depends_on = [helm_release.argocd]
}

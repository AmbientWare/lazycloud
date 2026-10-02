# Terraform installs node capacity and Argo CD; Argo CD installs everything
# else from deploy/argocd/apps on main.

locals {
  # The platform NodeClass and the Spot NodePool, applied with the Argo CD
  # release so the first nodes exist before any Argo CD application syncs.
  # Auto Mode chooses sizes from pod requests.
  node_capacity = [
    {
      apiVersion = "eks.amazonaws.com/v1"
      kind       = "NodeClass"
      metadata   = { name = "platform" }
      spec = {
        role                       = aws_iam_role.node.name
        subnetSelectorTerms        = [for subnet in aws_subnet.cluster : { id = subnet.id }]
        securityGroupSelectorTerms = [{ id = aws_eks_cluster.control_plane.vpc_config[0].cluster_security_group_id }]
        ephemeralStorage           = { size = "80Gi", iops = 3000, throughput = 125 }
      }
    },
    {
      apiVersion = "karpenter.sh/v1"
      kind       = "NodePool"
      metadata   = { name = "platform-spot" }
      spec = {
        template = {
          spec = {
            nodeClassRef = { group = "eks.amazonaws.com", kind = "NodeClass", name = "platform" }
            requirements = [
              { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot"] },
              { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
              { key = "kubernetes.io/os", operator = "In", values = ["linux"] },
              { key = "eks.amazonaws.com/instance-category", operator = "In", values = ["c", "m", "r"] },
              { key = "eks.amazonaws.com/instance-generation", operator = "Gt", values = ["4"] },
            ]
            expireAfter            = "336h"
            terminationGracePeriod = "24h"
          }
        }
        # Five minutes before consolidation rides out deployment bursts.
        disruption = { consolidationPolicy = "WhenEmptyOrUnderutilized", consolidateAfter = "5m", budgets = [{ nodes = "1" }] }
      }
    },
  ]

  # The one Application Terraform declares. It reads main, where each file
  # in deploy/argocd/apps is something this cluster runs. It ignores a
  # deployment Application's automated.enabled, so an operator's pause
  # survives syncs. A post-install hook, because the release's own CRDs must
  # exist first.
  root_application = {
    apiVersion = "argoproj.io/v1alpha1"
    kind       = "Application"
    metadata = {
      name        = "root"
      namespace   = "argocd"
      annotations = { "helm.sh/hook" = "post-install,post-upgrade", "helm.sh/hook-delete-policy" = "before-hook-creation" }
    }
    spec = {
      project     = "default"
      source      = { repoURL = "https://github.com/${var.github_repository}", targetRevision = "main", path = "deploy/argocd/apps" }
      destination = { server = "https://kubernetes.default.svc", namespace = "argocd" }
      ignoreDifferences = [{
        group = "argoproj.io", kind = "Application", jsonPointers = ["/spec/syncPolicy/automated/enabled"]
      }]
      syncPolicy = {
        automated   = { prune = true, selfHeal = true }
        syncOptions = ["CreateNamespace=true", "RespectIgnoreDifferences=true"]
      }
    }
  }

  # Requests for every Argo CD pod, so Auto Mode sizes nodes that carry them.
  argocd_resources = { for component, sizes in {
    server         = ["75m", "128Mi", "256Mi"]
    controller     = ["150m", "384Mi", "768Mi"]
    repoServer     = ["150m", "256Mi", "512Mi"]
    redis          = ["50m", "64Mi", "128Mi"]
    applicationSet = ["25m", "64Mi", "128Mi"]
    notifications  = ["25m", "64Mi", "128Mi"]
    dex            = ["25m", "64Mi", "128Mi"]
  } : component => { resources = { requests = { cpu = sizes[0], memory = sizes[1] }, limits = { memory = sizes[2] } } } }
}

resource "kubernetes_namespace_v1" "argocd" {
  metadata {
    name = "argocd"
  }
}

# Argo CD reads the organization's repositories as its GitHub App. Written
# here, not by External Secrets, which Argo CD itself installs.
resource "kubernetes_secret_v1" "argocd_repository" {
  metadata {
    name      = "github-repo-creds"
    namespace = kubernetes_namespace_v1.argocd.metadata[0].name
    labels    = { "argocd.argoproj.io/secret-type" = "repo-creds" }
  }
  data = {
    type                    = "git"
    url                     = "https://github.com/${split("/", var.github_repository)[0]}"
    githubAppID             = var.github_app_id
    githubAppInstallationID = var.github_app_installation_id
    githubAppPrivateKey     = var.github_app_private_key
  }
}

resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = var.argocd_chart_version
  namespace  = kubernetes_namespace_v1.argocd.metadata[0].name
  wait       = true
  timeout    = 900

  # The server stays cluster-internal; the deployment's cloudflared serves
  # argocd.<domain>.
  values = [yamlencode(merge(local.argocd_resources, {
    server       = merge(local.argocd_resources.server, { service = { type = "ClusterIP" }, extraArgs = ["--insecure"] })
    extraObjects = concat(local.node_capacity, [local.root_application])
  }))]

  depends_on = [
    kubernetes_secret_v1.argocd_repository,
    aws_eks_access_policy_association.node,
    aws_iam_role_policy_attachment.node,
  ]
}

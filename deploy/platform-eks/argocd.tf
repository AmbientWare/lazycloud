# The one controller Terraform installs, and the last thing it installs.
#
# Everything else that runs in this cluster is an Argo Application, declared in
# the repository and reconciled from it. Terraform's job ends at infrastructure
# plus the thing that reads git: a second system installing workloads is a second
# opinion about what should be running, and the disagreement surfaces as drift
# nobody owns.

resource "kubernetes_namespace" "argocd" {
  metadata {
    name = var.argocd_namespace
  }
}
resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = var.argocd_chart_version
  namespace  = kubernetes_namespace.argocd.metadata[0].name

  # The apply should mean the controller is running, not that the manifests were
  # accepted -- the root Application below is created against this cluster and
  # needs the CRDs to exist first.
  wait    = true
  timeout = 900

  values = [yamlencode({
    # Reachable through the cluster only. The tunnel serves the product, and
    # exposing a deployment controller beside it would publish the thing that can
    # change everything. Operators reach it with `kubectl port-forward`.
    #
    # Seven pods, and this chart declares a request for none of them. In a
    # cluster Karpenter sizes from requests that is not a rounding error: it is
    # seven pods the node is provisioned as though it does not carry, sharing
    # that node with the workloads it was sized for. Every one of them is given
    # a request below.
    #
    # `repo-server` and the application controller are the two with a spike.
    # Both run on every sync, and repo-server renders this repository's chart
    # while it does. A pod with no request holds the smallest share of a
    # contended CPU, which is where a controller misses its own liveness probe
    # and is killed for it, and a controller that cannot stay up cannot deploy
    # the change that would relieve the contention killing it.
    server = {
      service   = { type = "ClusterIP" }
      extraArgs = ["--insecure"]
      resources = {
        requests = { cpu = "75m", memory = "128Mi" }
        limits   = { memory = "256Mi" }
      }
    }

    controller = {
      resources = {
        requests = { cpu = "150m", memory = "384Mi" }
        limits   = { memory = "768Mi" }
      }
    }

    repoServer = {
      resources = {
        requests = { cpu = "150m", memory = "256Mi" }
        limits   = { memory = "512Mi" }
      }
    }

    redis = {
      resources = {
        requests = { cpu = "50m", memory = "64Mi" }
        limits   = { memory = "128Mi" }
      }
    }

    applicationSet = {
      resources = {
        requests = { cpu = "25m", memory = "64Mi" }
        limits   = { memory = "128Mi" }
      }
    }

    notifications = {
      resources = {
        requests = { cpu = "25m", memory = "64Mi" }
        limits   = { memory = "128Mi" }
      }
    }

    dex = {
      resources = {
        requests = { cpu = "25m", memory = "64Mi" }
        limits   = { memory = "128Mi" }
      }
    }

    # The only Application Terraform declares; everything else is a file in the
    # repository that this one finds.
    #
    # Carried by the release rather than as its own `kubernetes_manifest`,
    # because that resource reads the cluster's API at plan time and the cluster
    # does not exist when a fresh deployment is planned. Planning would fail on
    # the resource whose whole purpose is to run after the cluster exists, and
    # "one apply" would quietly become two.
    extraObjects = [{
      apiVersion = "argoproj.io/v1alpha1"
      kind       = "Application"
      metadata = {
        name      = "root"
        namespace = var.argocd_namespace
        annotations = {
          # After the release, not with it. Helm renders extra objects into the
          # same pass that installs the chart's CRDs, so an Application applied
          # there meets a cluster where `argoproj.io/v1alpha1` does not exist yet
          # and fails with "no matches for kind". A hook runs once the release's
          # own resources are in place.
          "helm.sh/hook"               = "post-install,post-upgrade"
          "helm.sh/hook-delete-policy" = "before-hook-creation"
        }
      }
      spec = {
        project = "default"
        source = {
          repoURL        = "https://github.com/${var.github_repository}"
          targetRevision = var.deployment_branch
          path           = "deploy/argocd/apps"
        }
        destination = {
          server    = "https://kubernetes.default.svc"
          namespace = var.argocd_namespace
        }
        syncPolicy = {
          automated = {
            # Both, deliberately. Without prune, deleting an Application from
            # the repository leaves it running with nothing declaring it;
            # without selfHeal, a change made with kubectl outlives the next
            # sync and the cluster stops matching what the repository says.
            prune    = true
            selfHeal = true
          }
          syncOptions = ["CreateNamespace=true"]
        }
      }
    }]
  })]

  # The credential has to exist before the root Application is reconciled, or the
  # first sync fails on a repository it cannot read and retries with a backoff
  # nobody is watching.
  depends_on = [kubernetes_secret.argocd_repository_credentials]
}

# How Argo reaches every repository in the organisation.
#
# The credential is the GitHub App already installed on AmbientWare rather than a
# deploy key: deploy keys are scoped to one repository, so the second repository
# that mattered would need a second credential and a second place to rotate it.
# Scoped to the organisation URL, so a new repository is reachable by existing.
#
# Written by Terraform rather than by External Secrets, because External Secrets
# is one of the things Argo installs and cannot also be what Argo needs to start.
#
# Supplied as a variable rather than read back from Secrets Manager, and the
# reason is ordering. Terraform declares that secret's container and never its
# value, so on a first apply the container exists and holds no version: a data
# source reading it fails, and the single apply this is meant to be becomes two
# with a manual step wedged between them. The operator already carries the
# PlanetScale, Cloudflare and Stripe credentials this way.

resource "kubernetes_secret" "argocd_repository_credentials" {
  metadata {
    name      = "ambientware-repo-creds"
    namespace = kubernetes_namespace.argocd.metadata[0].name

    labels = {
      "argocd.argoproj.io/secret-type" = "repo-creds"
    }
  }

  data = {
    type                    = "git"
    url                     = "https://github.com/${var.github_organization}"
    githubAppID             = var.github_app_id
    githubAppInstallationID = var.github_app_installation_id
    githubAppPrivateKey     = var.github_app_private_key
  }

}

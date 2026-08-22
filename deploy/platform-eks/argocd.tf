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
    server = {
      service   = { type = "ClusterIP" }
      extraArgs = ["--insecure"]
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
data "aws_secretsmanager_secret_version" "github_app_private_key" {
  secret_id = aws_secretsmanager_secret.runtime["github-app-private-key"].id
}

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
    githubAppPrivateKey     = data.aws_secretsmanager_secret_version.github_app_private_key.secret_string
  }

}


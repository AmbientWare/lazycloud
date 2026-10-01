variable "name" {
  description = <<-EOT
    Name of the cluster, and the prefix of every resource this module creates.

    One cluster carries every deployment, so this is not a deployment name. The
    deployments carry their own (`lazycloud-prod`, `lazycloud-staging`) in
    `deploy/platform-deployment`, and each is a namespace here.
  EOT
  type        = string
  default     = "lazycloud"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name))
    error_message = "name must be 3-31 lowercase alphanumeric characters or dashes, starting with a letter."
  }
}

variable "region" {
  description = "Region holding the cluster. Every deployment on it uses the same one."
  type        = string
  default     = "us-east-1"
}

variable "control_plane_cidr" {
  description = "CIDR of the VPC holding the cluster."
  type        = string
  default     = "10.80.0.0/16"
}

variable "cluster_subnet_count" {
  description = <<-EOT
    Availability zones the cluster spans.

    Two is EKS's own minimum. A third costs nothing while no nodes run in it and
    gives the scheduler somewhere to place a pod when a zone is degraded.
  EOT
  type        = number
  default     = 3
}

variable "kubernetes_version" {
  description = <<-EOT
    EKS control plane version, and a billing decision as much as a technical one.

    A cluster past its standard support date keeps running and costs roughly six
    times the hourly rate, which is a change nothing in the console announces.
    Check `aws eks describe-cluster-versions` before pinning: the value here
    should be the newest version offered, not the newest one remembered.
  EOT
  type        = string
  default     = "1.36"
}

variable "cluster_api_cidrs" {
  description = <<-EOT
    Addresses allowed to reach the Kubernetes API from outside the VPC, as CIDR
    blocks. The machine `terraform apply` runs from belongs here, because the
    Kubernetes and Helm providers reach the cluster through this endpoint. Empty
    disables the public endpoint.

    Only this module needs it. A deployment apply never reaches the cluster's
    API, so a deployment can be applied from anywhere.

    When that machine's address changes, move the cluster alone:
    `terraform apply -target=aws_eks_cluster.control_plane` rewrites the list
    through the AWS API without the providers having to reach an endpoint that
    now refuses them. A full apply follows.
  EOT
  type        = list(string)

  validation {
    condition     = alltrue([for cidr in var.cluster_api_cidrs : can(cidrnetmask(cidr))])
    error_message = "cluster_api_cidrs must be IPv4 CIDR blocks, for example 203.0.113.7/32."
  }
}

variable "destroy_repositories_with_images" {
  description = <<-EOT
    Let `terraform destroy` remove image repositories that still hold images.

    True while predeployment, so the whole platform can be torn down and rebuilt
    to prove it reproduces. Set it false once a deployment runs images a
    customer's node still pulls by digest.
  EOT
  type        = bool
  default     = true
}

variable "storage_class_name" {
  description = "Name of the EBS storage class every deployment's claims name."
  type        = string
  default     = "lazycloud-ebs"
}

variable "github_repository" {
  description = "owner/repo Argo's root Application reads the deployment list from."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "github_organization" {
  description = "Organisation whose repositories Argo may read."
  type        = string
  default     = "AmbientWare"
}

variable "argocd_namespace" {
  description = "Namespace Argo CD runs in."
  type        = string
  default     = "argocd"
}

variable "argocd_chart_version" {
  description = <<-EOT
    Argo CD chart version, pinned.

    A controller that reconciles everything else is the last thing that should
    move on its own: an unpinned upgrade changes how every other workload is
    applied, at whatever moment the next apply happens to run.

    Pinned to a version that knows the cluster's Kubernetes. Argo builds a typed
    diff from the live resource, so one older than the API server fails on fields
    it has never heard of -- `.status.terminatingReplicas` here -- and reports it
    as a comparison error rather than as its own age. Check
    `helm search repo argo/argo-cd --versions` when moving Kubernetes.
  EOT
  type        = string
  default     = "10.4.0"
}

variable "github_app_id" {
  description = "GitHub App id. The AmbientWare App is 3246255."
  type        = string
  default     = "3246255"
}

variable "github_app_installation_id" {
  description = "Installation id of that App on the organisation."
  type        = string
  default     = "124042395"
}

variable "github_app_private_key" {
  description = <<-EOT
    PEM for the organisation's GitHub App, which is how Argo reads the repository.

    Supplied from the operator environment as `TF_VAR_github_app_private_key`.
    Generated in the App's settings and not readable back from GitHub, so a lost
    key is replaced rather than recovered.
  EOT
  type        = string
  sensitive   = true
}

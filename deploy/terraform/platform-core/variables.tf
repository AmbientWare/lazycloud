variable "name" {
  description = "Cluster name and the prefix of every resource here. One cluster carries every deployment."
  type        = string
  default     = "lazycloud"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.name))
    error_message = "name must be 3-31 lowercase alphanumeric characters or dashes, starting with a letter."
  }
}

variable "region" {
  description = "Region of the cluster and of every deployment on it."
  type        = string
  default     = "us-east-1"
}

variable "cidr" {
  description = "CIDR of the cluster VPC. Deployments' fleet networks must not overlap it."
  type        = string
  default     = "10.80.0.0/16"
}

variable "zones" {
  description = "Availability zones the cluster spans; EKS needs two, a third keeps a replica placeable when one degrades."
  type        = number
  default     = 3
}

variable "kubernetes_version" {
  description = "EKS version. Past standard support a cluster costs about six times as much; check `aws eks describe-cluster-versions`."
  type        = string
  default     = "1.36"
}

variable "cluster_api_cidrs" {
  description = "Addresses that may reach the Kubernetes API from outside the VPC, including the machine applying this root. Empty closes the public endpoint."
  type        = list(string)

  validation {
    condition     = alltrue([for cidr in var.cluster_api_cidrs : can(cidrnetmask(cidr))])
    error_message = "cluster_api_cidrs must be IPv4 CIDR blocks, for example 203.0.113.7/32."
  }
}

variable "github_repository" {
  description = "owner/repo the root Argo CD Application reads deploy/argocd/apps from."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "argocd_chart_version" {
  description = "argo-cd chart version. It must know the cluster's Kubernetes version."
  type        = string
  default     = "10.9.6"
}

variable "github_app_id" {
  description = "GitHub App Argo CD reads the organization's repositories with."
  type        = string
  default     = "3246255"
}

variable "github_app_installation_id" {
  description = "Installation of that App on the organization."
  type        = string
  default     = "124042395"
}

variable "github_app_private_key" {
  description = "PEM of that App, from TF_VAR_github_app_private_key. GitHub cannot show it again; a lost key is replaced."
  type        = string
  sensitive   = true
}

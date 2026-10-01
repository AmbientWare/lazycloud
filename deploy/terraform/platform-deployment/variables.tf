variable "deployment" {
  description = <<-EOT
    Name of this installation. Prefixes every globally-named resource and
    secret path, names the PlanetScale databases, tags the fleet's instances
    and is the Kubernetes namespace the deployment runs in.

    `lazycloud-prod` and `lazycloud-staging` share one cluster and one AWS
    account, and this is the one word that tells every resource apart.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.deployment))
    error_message = "deployment must be 3-31 lowercase alphanumeric characters or dashes, starting with a letter."
  }
}

variable "region" {
  description = "Region holding the cluster. Must match the cluster's."
  type        = string
  default     = "us-east-1"
}

variable "core_state_key" {
  description = "State key of the platform-core root this deployment attaches to."
  type        = string
  default     = "platform-core/lazycloud.tfstate"
}

variable "github_environment" {
  description = <<-EOT
    GitHub Actions environment whose jobs may assume this deployment's deploy
    role: `prod` or `staging`. The Deploy workflow runs its job under the
    environment named for the deployment it targets, and the role's trust
    names the same one, so the environment is the whole of what separates a
    staging deploy from a prod one.
  EOT
  type        = string

  validation {
    condition     = contains(["prod", "staging"], var.github_environment)
    error_message = "github_environment is prod or staging."
  }
}

variable "github_repository" {
  description = "owner/repo the Deploy workflow runs from, for the deploy role's trust condition."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "terraform_backend_config" {
  description = "Absolute path to the operator's S3 backend JSON used by terraform init and remote state readers. Contains coordinates and profile, never credentials."
  type        = string

  validation {
    condition     = startswith(var.terraform_backend_config, "/") && can(jsondecode(file(var.terraform_backend_config)))
    error_message = "terraform_backend_config must name an absolute path to a valid backend JSON file."
  }
}

variable "cloudflare_state_key" {
  description = "State key of the cloudflare root, read for the tunnel credentials and zone."
  type        = string
  default     = "cloudflare/production.tfstate"
}

variable "planetscale_organization" {
  description = "PlanetScale organization owning the databases."
  type        = string
}

variable "planetscale_major_version" {
  description = "Postgres major version. Pinned rather than tracking latest."
  type        = string
  default     = "17"
}

variable "planetscale_cluster_size" {
  description = <<-EOT
    Cluster size for each branch.

    `PS_10_AWS_ARM` is the smallest production tier in AWS: 1/8 vCPU, 1 GiB.
    `PS_DEV_AWS_ARM` is smaller still and is the right choice for a
    non-production deployment. The organization's SKU list spells these
    without the suffix; the provider rejects that form at plan.
  EOT
  type        = string
  default     = "PS_10_AWS_ARM"
}

variable "planetscale_region" {
  description = "PlanetScale region. Keep it beside the cluster."
  type        = string
  default     = "us-east"
}

variable "platform_database" {
  description = <<-EOT
    PlanetScale database of the platform: a fresh schema, apart from the
    reference platform's `<deployment>` database. Unset, `<deployment>-platform`.
  EOT
  type        = string
  default     = null
  nullable    = true
}

variable "database_max_connections" {
  description = "PostgreSQL server connection ceiling of each database branch."
  type        = number
  default     = 40
  validation {
    condition     = var.database_max_connections > 0 && floor(var.database_max_connections) == var.database_max_connections
    error_message = "database_max_connections must be a positive integer."
  }
}

variable "database_pool_max_connections" {
  description = <<-EOT
    pool_max_conns of every server, scheduler and job process. Replicas plus
    surge times this, plus a reserve, must stay under database_max_connections:
    three servers, two schedulers and one job at 6 is 36 of 40.
  EOT
  type        = number
  default     = 6
  validation {
    condition     = var.database_pool_max_connections >= 4 && floor(var.database_pool_max_connections) == var.database_pool_max_connections
    error_message = "database_pool_max_connections must be an integer of at least 4: each process holds a listener and a lock connection."
  }
}

variable "fleet_cidr" {
  description = <<-EOT
    CIDR of each regional VPC the fleet launches into.

    Must not overlap the cluster's 10.80.0.0/16 or the 10.86.0.0/16 a customer
    connection stack creates, since a connected account can be this account.
  EOT
  type        = string
  default     = "10.84.0.0/16"
}

variable "platform_service_accounts" {
  description = <<-EOT
    Service accounts of the chart (deploy/helm/lazycloud) that hold the
    control plane's AWS identity through Pod Identity.

    Named exactly rather than by wildcard: this role launches the fleet,
    reaches every workspace bucket and is the principal customer connection
    roles trust, so "any pod in the namespace" is a larger grant than it
    looks.
  EOT
  type = object({
    server    = string
    scheduler = string
  })
  default = { server = "lazycloud-server", scheduler = "lazycloud-scheduler" }
}

variable "secrets_reader_service_account" {
  description = <<-EOT
    Service account the deployment's External Secrets store presents. Named in
    the reader role's trust as `system:serviceaccount:<deployment>:<this>`, so
    the chart's secrets-reader service account must say the same.
  EOT
  type        = string
  default     = "secrets-reader"
}

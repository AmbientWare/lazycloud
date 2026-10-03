variable "deployment" {
  description = "Name of the installation: its namespace, the PlanetScale database, the fleet tag and the prefix of every AWS name and secret path."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.deployment))
    error_message = "deployment must be 3-31 lowercase alphanumeric characters or dashes, starting with a letter."
  }
}

variable "github_environment" {
  description = "GitHub environment whose Deploy job may assume this deployment's deploy role."
  type        = string

  validation {
    condition     = contains(["prod", "staging"], var.github_environment)
    error_message = "github_environment is prod or staging."
  }
}

variable "domain" {
  description = "Zone apex the deployment answers on, such as lazycloud.dev."
  type        = string
}

variable "cloudflare_account_id" {
  description = "Cloudflare account that owns the tunnel."
  type        = string
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone of domain. This root writes named records only and never touches the others, mail among them."
  type        = string
}

variable "planetscale_organization" {
  description = "PlanetScale organization."
  type        = string
}

variable "planetscale_cluster_size" {
  description = "Cluster size of the branch. PS_10_AWS_ARM is the smallest production size in AWS; the provider rejects the SKU spelling without the suffix."
  type        = string
  default     = "PS_10_AWS_ARM"
}

variable "planetscale_region" {
  description = "PlanetScale region slug beside the cluster (`pscale region list`)."
  type        = string
  default     = "us-east"
}

variable "terraform_backend_config" {
  description = "Absolute path of the operator's backend JSON, which also locates platform-core's state."
  type        = string
}

variable "region" {
  description = "Region of the cluster. Must match platform-core's."
  type        = string
  default     = "us-east-1"
}

variable "fleet_cidr" {
  description = "CIDR of each regional fleet VPC. It must overlap neither the cluster's nor another deployment's, nor the 10.86.0.0/16 a customer connection stack creates."
  type        = string
  default     = "10.84.0.0/16"
}

variable "database_pool_max_connections" {
  description = "pool_max_conns of each of a process's two pools, one per database URL; the session pool holds the listener and the locks."
  type        = number
  default     = 4
}

variable "database_max_connections" {
  description = "max_connections of the branch; database.tf budgets the processes against it."
  type        = number
  default     = 60
}

# The chart creates the two load balancers; their hostnames arrive after the
# first sync and a second apply points the records at them.
variable "host_load_balancer" {
  description = "Hostname of the server-hosts NLB, which hosts.<domain> points at."
  type        = string
  default     = null
}

variable "tcp_load_balancer" {
  description = "Hostname of the server-tcp NLB, which *.tcp.<domain> points at."
  type        = string
  default     = null
}

variable "github_repository" {
  description = "owner/repo whose Deploy workflow holds the deploy role."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "core_state_key" {
  type    = string
  default = "platform-core/lazycloud.tfstate"
}

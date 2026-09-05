variable "deployment" {
  description = <<-EOT
    Name of this installation. Prefixes every globally-named resource and
    secret path, names the PlanetScale database, and is the Kubernetes
    namespace the deployment runs in.

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
  description = "Region holding the cluster and the shared fleet. Must match the cluster's."
  type        = string
  default     = "us-east-1"
}

variable "core_state_key" {
  description = "State key of the deploy/platform-core module this deployment attaches to."
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

variable "planetscale_organization" {
  description = "PlanetScale organization owning the control plane database."
  type        = string
}

variable "planetscale_major_version" {
  description = "Postgres major version. Pinned rather than tracking latest."
  type        = string
  default     = "17"
}

variable "planetscale_cluster_size" {
  description = <<-EOT
    Cluster size for the branch.

    `PS_10_AWS_ARM` is the smallest production tier in AWS: 1/8 vCPU, 1 GiB. The
    name carries the provider and the host architecture, which is PlanetScale's
    hardware and has nothing to do with what connects to it. `PS_DEV_AWS_ARM` is
    smaller still and is the right choice for a non-production deployment.

    The organization's SKU list spells these without the suffix; the provider
    rejects that form, and an invalid one fails at plan rather than at apply.
  EOT
  type        = string
  default     = "PS_10_AWS_ARM"
}

variable "planetscale_region" {
  description = "PlanetScale region. Keep it beside the control plane host."
  type        = string
  default     = "us-east"
}

variable "create_acceptance_roles" {
  description = <<-EOT
    Create the acceptance operator, stack execution and node diagnostics roles.

    True only for an account that runs the live acceptance scenarios. A production
    deployment has no use for them, and the diagnostics role in particular is
    remote code execution on a running node.
  EOT
  type        = bool
  default     = false
}

variable "acceptance_trusted_principal_arns" {
  description = <<-EOT
    Principals permitted to assume the acceptance roles.

    Exact principals. An account root here would let any principal in the account
    assume them.
  EOT
  type        = list(string)
  default     = []
}

variable "github_repository" {
  description = "owner/repo the Deploy workflow runs from, for the deploy role's trust condition."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "state_bucket" {
  description = "Bucket holding every module's Terraform state: this one's, the core's it reads, and the cloudflare module's."
  type        = string
}

variable "destroy_buckets_with_contents" {
  description = <<-EOT
    Let `terraform destroy` remove buckets that still hold data.

    True while predeployment, so the whole deployment can be torn down and rebuilt
    to prove it reproduces. Set it false once these hold anything a customer would
    miss; unlike `prevent_destroy`, this is a variable and can be switched.
  EOT
  type        = bool
  default     = true
}

variable "cloudflare_state_key" {
  description = "State key of the deploy/cloudflare module, read for the tunnel credentials."
  type        = string
  default     = "cloudflare/production.tfstate"
}

variable "control_role_name" {
  description = <<-EOT
    Name of the platform control principal.

    A durable external contract, not a label: a customer's authorization template
    writes this role's ARN into every connection role's trust policy. Change it
    only for a deployment that has no existing customer connections.

    It said `compose` until nothing external depended on it. The name came from
    the local stack and then stood in production, where it described neither what
    the role is nor where it runs, and the one error that named it read as a
    misconfiguration rather than as the caller being wrong.

    Unset, it is `<deployment>-control-principal`, which keeps two deployments
    in one account apart. Set it only to keep a name customers already trust.
  EOT
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.control_role_name == null || can(regex("^[A-Za-z0-9_+=,.@-]{1,64}$", var.control_role_name))
    error_message = "control_role_name must be a valid IAM role name."
  }
}

variable "fleet_cidr" {
  description = <<-EOT
    CIDR of the VPC the shared fleet launches into.

    Must not overlap control_plane_cidr, and must not overlap the 10.86.0.0/16 a
    customer authorization stack creates: a connected account can be this account
    during acceptance, and two overlapping VPCs there are a routing problem
    nobody sees until a node cannot reach the control plane.
  EOT
  type        = string
  default     = "10.84.0.0/16"
}

variable "control_plane_service_accounts" {
  description = <<-EOT
    Service accounts permitted to assume the control plane's AWS identity.

    Named exactly rather than by wildcard: this role reaches every workspace
    bucket and the connected-AWS control role trusts it, so "any pod in the
    namespace" is a larger grant than it looks.
  EOT
  type = object({
    controlPlane = string
    scheduler    = string
  })
  default = { controlPlane = "control-plane", scheduler = "scheduler" }
}

variable "wireguard_bootstrap_service_account" {
  description = "Service account permitted to initialize the deployment's WireGuard key document."
  type        = string
  default     = "wireguard-bootstrap"
}

variable "secrets_reader_service_account" {
  description = <<-EOT
    Service account the deployment's External Secrets store presents. Named in
    the reader role's trust as `system:serviceaccount:<deployment>:<this>`, so
    the chart's `serviceAccounts.secretsReader` must say the same.
  EOT
  type        = string
  default     = "secrets-reader"
}

variable "redis_node_type" {
  description = "ElastiCache node type for the coordination Redis."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_engine_version" {
  description = "ElastiCache Redis engine version."
  type        = string
  default     = "7.1"
}

variable "database_max_connections" {
  description = "PostgreSQL server connection ceiling, exported for Helm pool budgeting."
  type        = number
  default     = 40
  validation {
    condition     = var.database_max_connections > 0 && floor(var.database_max_connections) == var.database_max_connections
    error_message = "database_max_connections must be a positive integer."
  }
}

variable "database_pooler_max_connections" {
  description = "Local PgBouncer server connections per database, exported for deployment budgeting."
  type        = number
  default     = 20
  validation {
    condition     = var.database_pooler_max_connections > 0 && floor(var.database_pooler_max_connections) == var.database_pooler_max_connections
    error_message = "database_pooler_max_connections must be a positive integer."
  }
}

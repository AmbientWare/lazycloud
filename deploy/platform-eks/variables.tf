variable "deployment" {
  description = "Name distinguishing this installation. Prefixes every globally-named resource."
  type        = string
  default     = "lazycloud-prod"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,30}$", var.deployment))
    error_message = "deployment must be 3-31 lowercase alphanumeric characters or dashes, starting with a letter."
  }
}

variable "region" {
  description = "Region holding the control plane and the shared fleet."
  type        = string
  default     = "us-east-1"
}

variable "control_plane_cidr" {
  description = "CIDR of the VPC holding the control plane."
  type        = string
  default     = "10.80.0.0/16"
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
  description = "owner/repo the Deploy workflow runs from, for the OIDC trust condition."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "state_bucket" {
  description = "Bucket holding this module's Terraform state, which the workflow reads for outputs."
  type        = string
}

variable "destroy_buckets_with_contents" {
  description = <<-EOT
    Let `terraform destroy` remove buckets and repositories that still hold data.

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

    It is also the one name that does not carry the deployment prefix, so two
    deployments sharing an AWS account must give it different values. A
    non-production deployment can pick freely, because the contract only binds
    where customers already connected.
  EOT
  type        = string
  default     = "lazycloud-compose-control"

  validation {
    condition     = can(regex("^[A-Za-z0-9_+=,.@-]{1,64}$", var.control_role_name))
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

variable "instance_hourly_micros" {
  description = <<-EOT
    Hourly price per instance type, in millionths of a dollar.

    Empty by default, and that default is load-bearing. This is the one managed
    capacity value a release cannot publish, so authoring it is what declares
    that a deployment intends to run managed AWS capacity at all; supplying it
    here for everyone would make the declaration meaningless and would demand a
    release from deployments that have none. `terraform.tfvars.example` carries
    a current us-east-1 map to paste in.

    It is also the ranking `choose_offer` uses, so a wrong figure does not fail.
    It silently changes which instance every workload lands on. Leave out any
    type whose on-demand rate is not published rather than recording a zero:
    zero wins every comparison.
  EOT
  type        = map(number)
  default     = {}
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

variable "kubernetes_namespace" {
  description = <<-EOT
    Namespace the deployment's workloads run in.

    Its own, because the control plane pod needs NET_ADMIN, NET_RAW and a TUN
    device, which a restrictive Pod Security Standard refuses. Confining that
    exception to one namespace is what keeps it from applying to everything.
  EOT
  type        = string
  default     = "lazycloud"
}

variable "control_plane_service_accounts" {
  description = <<-EOT
    Service accounts permitted to assume the control plane's AWS identity.

    Named exactly rather than by wildcard: this role reaches every workspace
    bucket and the connected-AWS control role trusts it, so "any pod in the
    namespace" is a larger grant than it looks.
  EOT
  type        = list(string)
  default     = ["control-plane", "scheduler"]
}

variable "external_secrets_service_account" {
  description = "Service account the External Secrets Operator runs as."
  type        = string
  default     = "external-secrets"
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

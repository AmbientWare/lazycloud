variable "name" {
  description = "Platform name; repositories are named <name>/<image>, as in the existing platform-core repositories."
  type        = string
  default     = "lazycloud"
}

variable "region" {
  description = "Region of the platform cluster and its ECR registry (platform-core's region)."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "owner/name of the repository whose release workflow pushes images."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "github_environment" {
  description = <<-EOT
    GitHub environment the release job runs in. The role trusts only jobs in
    it, so its deployment rule (tags v* only) decides which refs can push.
  EOT
  type        = string
  default     = "images"
}

variable "destroy_repositories_with_images" {
  description = "Let destroy delete repositories that still hold images."
  type        = bool
  default     = false
}

variable "name" {
  description = "Platform name. Repositories are <name>/release/<image>, apart from the reference platform's <name>/<image> repositories."
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

variable "release_reviewer_user_ids" {
  description = "GitHub user ids who must approve each image release job in the images environment."
  type        = list(number)

  validation {
    condition     = length(var.release_reviewer_user_ids) > 0
    error_message = "At least one release reviewer is required."
  }
}

variable "accept_repository_subject_change" {
  description = <<-EOT
    The OIDC subject template this root sets applies to every workflow in
    the repository, so the reference platform's deploy and release roles,
    which match repo:<repository>:environment:<name>, stop matching. Set true
    only after their trust policies match the new subject (see the subject
    format in main.tf), or Ship to production fails.
  EOT
  type        = bool
  default     = false

  validation {
    condition     = var.accept_repository_subject_change
    error_message = "Update the reference deploy and release role trusts for the new OIDC subject first, then set accept_repository_subject_change = true."
  }
}

variable "destroy_repositories_with_images" {
  description = "Let destroy delete repositories that still hold images."
  type        = bool
  default     = false
}

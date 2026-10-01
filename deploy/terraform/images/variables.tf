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
  description = "owner/name of the repository whose Ship and Node images workflows hold the roles."
  type        = string
  default     = "AmbientWare/lazycloud"
}

variable "release_reviewer_user_ids" {
  description = "GitHub user ids who must approve each image push and node image bake in the images environment."
  type        = list(number)

  validation {
    condition     = length(var.release_reviewer_user_ids) > 0
    error_message = "At least one release reviewer is required."
  }
}

variable "accept_repository_subject_change" {
  description = <<-EOT
    The OIDC subject template this root sets applies to every workflow in
    the repository, so roles that match repo:<repository>:environment:<name>
    stop matching: main's deploy role (until platform-deployment trusts both
    subjects) and its release role (the lazycloud-release-assets stack). Set
    true only after platform-deployment is applied with the dual trust and
    the cutover no longer needs main's Ship (tasks/deploy.md).
  EOT
  type        = bool
  default     = false

  validation {
    condition     = var.accept_repository_subject_change
    error_message = "Apply platform-deployment with its dual deploy trust first, then set accept_repository_subject_change = true (tasks/deploy.md)."
  }
}

variable "destroy_repositories_with_images" {
  description = "Let destroy delete repositories that still hold images."
  type        = bool
  default     = false
}

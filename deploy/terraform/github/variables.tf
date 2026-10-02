variable "name" {
  description = "Platform name, the prefix of the workflow roles."
  type        = string
  default     = "lazycloud"
}

variable "deployment" {
  description = "The deployment whose deploy role the prod environment holds."
  type        = string
  default     = "lazycloud-prod"
}

variable "github_repository" {
  type    = string
  default = "AmbientWare/lazycloud"
}

variable "release_reviewer_user_ids" {
  description = "GitHub user ids, at least one, who approve each image push and node image bake."
  type        = list(number)

  validation {
    condition     = length(var.release_reviewer_user_ids) > 0
    error_message = "At least one release reviewer is required."
  }
}

variable "terraform_backend_config" {
  description = "Absolute path of the operator's backend JSON, which also locates the other roots' state."
  type        = string
}

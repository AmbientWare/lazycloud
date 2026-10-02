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

variable "terraform_backend_config" {
  description = "Absolute path of the operator's backend JSON, which also locates the other roots' state."
  type        = string
}

# -----------------------------------------------------------------------------
# Global Variables
# -----------------------------------------------------------------------------

variable "aws_region" {
  description = "AWS region for Secrets Manager"
  type        = string
  default     = "us-east-1"
}

variable "secrets_prefix" {
  description = "Prefix for AWS Secrets Manager secret names"
  type        = string
  default     = "lazycloud"
}

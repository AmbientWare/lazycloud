# The private bucket every other root keeps its state in. This root keeps
# its own state locally; the bucket is the only thing it owns, and nothing
# is lost if that file is: the bucket stays, and so do the states in it.
# Applied once, before any other root.

terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.67.0"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "state" {
  bucket = "lazycloud-state-${data.aws_caller_identity.current.account_id}"

  lifecycle {
    prevent_destroy = true
  }
}

# Versions keep every earlier state, so a bad apply can be undone by hand.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "backend" {
  description = "Write this to the operator's backend file."
  value = {
    bucket       = aws_s3_bucket.state.id
    region       = var.region
    profile      = "default"
    encrypt      = true
    use_lockfile = true
  }
}

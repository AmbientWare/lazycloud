terraform {
  required_version = ">= 1.16.0, < 2.0.0"

  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.67.0"
    }
    github = {
      source  = "integrations/github"
      version = "= 6.13.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
  default_tags {
    tags = { "lazycloud:platform" = var.name, "lazycloud:managed-by" = "terraform" }
  }
}

# Reads GITHUB_TOKEN, a repository administrator's token, from the environment.
provider "github" {
  owner = split("/", var.github_repository)[0]
}

# S3 Backend Configuration for ash-1 cluster
# Usage: terraform init -backend-config=backend.hcl

bucket = "lazycloud-terraform-state"
key    = "clusters/ash-1/terraform.tfstate"
region = "us-east-1"

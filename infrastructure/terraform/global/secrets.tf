# -----------------------------------------------------------------------------
# AWS Secrets Manager — Shared Secrets
# -----------------------------------------------------------------------------
# These secrets are shared across all clusters and consumed by External Secrets
# Operator. Terraform creates the secret containers. Populate values after apply:
#
#   aws secretsmanager put-secret-value --secret-id lazycloud/prod-secrets \
#     --secret-string file://infrastructure/secrets-backup/lazycloud-prod-secrets.json
#
#   aws secretsmanager put-secret-value --secret-id lazycloud/shared-secrets \
#     --secret-string file://infrastructure/secrets-backup/lazycloud-shared-secrets.json
#
#   aws secretsmanager put-secret-value --secret-id lazycloud/staging-secrets \
#     --secret-string file://infrastructure/secrets-backup/lazycloud-staging-secrets.json
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "prod" {
  name        = "${var.secrets_prefix}/prod-secrets"
  description = "LazyCloud production application secrets"

  tags = {
    environment = "prod"
    managed_by  = "terraform"
  }
}

resource "aws_secretsmanager_secret" "shared" {
  name        = "${var.secrets_prefix}/shared-secrets"
  description = "LazyCloud shared secrets - Cloudflare tunnel, Depot registry, etc."

  tags = {
    environment = "shared"
    managed_by  = "terraform"
  }
}

resource "aws_secretsmanager_secret" "staging" {
  name        = "${var.secrets_prefix}/staging-secrets"
  description = "LazyCloud staging application secrets"

  tags = {
    environment = "staging"
    managed_by  = "terraform"
  }
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "prod_secret_arn" {
  description = "ARN of the production secrets"
  value       = aws_secretsmanager_secret.prod.arn
}

output "shared_secret_arn" {
  description = "ARN of the shared secrets"
  value       = aws_secretsmanager_secret.shared.arn
}

output "staging_secret_arn" {
  description = "ARN of the staging secrets"
  value       = aws_secretsmanager_secret.staging.arn
}

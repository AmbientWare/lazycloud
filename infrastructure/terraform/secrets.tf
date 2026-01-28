# -----------------------------------------------------------------------------
# AWS Secrets Manager — consumed by External Secrets Operator on the cluster
#
# Terraform creates the secret containers. Populate values after apply:
#
#   aws secretsmanager put-secret-value --secret-id lazycloud/prod-secrets \
#     --secret-string file://../../infrastructure/secrets-backup/lazycloud-prod-secrets.json
#
#   aws secretsmanager put-secret-value --secret-id lazycloud/shared-secrets \
#     --secret-string file://../../infrastructure/secrets-backup/lazycloud-shared-secrets.json
#
#   aws secretsmanager put-secret-value --secret-id lazycloud/staging-secrets \
#     --secret-string file://../../infrastructure/secrets-backup/lazycloud-staging-secrets.json
# -----------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "prod" {
  name        = "lazycloud/prod-secrets"
  description = "LazyCloud production application secrets"
}

resource "aws_secretsmanager_secret" "shared" {
  name        = "lazycloud/shared-secrets"
  description = "LazyCloud shared secrets (Cloudflare tunnel, Depot registry)"
}

resource "aws_secretsmanager_secret" "staging" {
  name        = "lazycloud/staging-secrets"
  description = "LazyCloud staging application secrets"
}

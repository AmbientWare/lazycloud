# Reads the secret values this deployment's containers are given.
#
# Held by a service account in this deployment's namespace, not by the External
# Secrets operator. The operator is installed once and reconciles every
# namespace's SecretStore, so an identity it held would be one identity for all
# of them, and "staging cannot read prod's documents" would be a convention in
# a chart rather than a fact in IAM.
#
# IRSA rather than Pod Identity, because the store's identity is a token
# exchange: the operator mints a token for the service account the store names
# and presents it to STS, and there is no pod behind that token for Pod Identity
# to attach to. The trust condition on the subject is what scopes it -- a store
# in `lazycloud-staging` cannot mint a token whose subject says `lazycloud-prod`.
#
# Scoped to this deployment's prefix and to reading. External Secrets copies
# values into the cluster; it never writes one back, and a role that could would
# make the cluster an authority on credentials Terraform declares.
resource "aws_iam_role" "secrets_reader" {
  name        = "${var.deployment}-secrets-reader"
  description = "External Secrets store for ${var.deployment}: reads this deployment's secret values."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_issuer_host}:aud" = "sts.amazonaws.com"
          "${local.oidc_issuer_host}:sub" = "system:serviceaccount:${var.deployment}:${var.secrets_reader_service_account}"
        }
      }
    }]
  })
}

data "aws_iam_policy_document" "secrets_reader" {
  statement {
    sid       = "ReadDeploymentSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["${local.arn_prefix}:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${var.deployment}/*"]
  }

  statement {
    sid       = "FindThemByName"
    actions   = ["secretsmanager:ListSecrets"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "secrets_reader" {
  name   = "secrets-reader"
  role   = aws_iam_role.secrets_reader.id
  policy = data.aws_iam_policy_document.secrets_reader.json
}

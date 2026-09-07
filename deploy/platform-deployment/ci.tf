# The identity the Deploy workflow assumes. No long-lived key exists for it: the
# workflow presents a GitHub OIDC token and AWS exchanges it for a session.
#
# The provider is referenced rather than declared. It is account-wide, shared with
# whatever else in this account uses GitHub Actions, and declaring it here would
# make this module the owner of something it did not create.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "deploy" {
  name        = "${var.deployment}-deploy"
  description = "GitHub Actions deploy identity for ${var.deployment}."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Scoped to the environment as well as the repository. A workflow job
          # without this deployment's `environment:` gets a different sub claim
          # and cannot assume this role, so a pull request from a fork cannot
          # reach the deployment even if it can run a workflow, and a deploy to
          # staging cannot write prod's bundle.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.github_environment}"
        }
      }
    }]
  })
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "AuthenticateToRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "AuthenticateToReleaseRegistry"
    actions   = ["ecr-public:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "ReleaseRegistryBearerToken"
    actions   = ["sts:GetServiceBearerToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushDeploymentImages"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = local.ecr_repository_arns
  }

}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.name
  policy = data.aws_iam_policy_document.deploy.json
}

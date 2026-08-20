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
          # Scoped to the environment, not just the repository. A workflow without
          # `environment: production` gets a different sub claim and cannot assume
          # this role, so a pull request from a fork cannot reach the deployment
          # even if it can run a workflow.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:production"
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
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }

  statement {
    sid       = "WriteTheBundle"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.deploy.arn, "${aws_s3_bucket.deploy.arn}/*"]
  }

  # `terraform output` reads state. The workflow never applies, so this is read
  # and the lock, not write.
  statement {
    sid     = "ReadDeploymentState"
    actions = ["s3:GetObject", "s3:ListBucket"]
    resources = [
      "${local.arn_prefix}:s3:::${var.state_bucket}",
      "${local.arn_prefix}:s3:::${var.state_bucket}/*",
    ]
  }

  # Converging the host. Scoped to the one instance and the one document, so this
  # identity cannot run a command anywhere else in the account.
  statement {
    sid     = "ConvergeTheControlPlane"
    actions = ["ssm:SendCommand"]
    resources = [
      aws_instance.control_plane.arn,
      "${local.arn_prefix}:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }

  statement {
    sid       = "ReadCommandResult"
    actions   = ["ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.name
  policy = data.aws_iam_policy_document.deploy.json
}

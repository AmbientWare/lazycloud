# The identity the Deploy workflow assumes. No long-lived key exists for it: the
# workflow presents a GitHub OIDC token and AWS exchanges it for a session.
#
# The provider is referenced rather than declared. It is account-wide, shared with
# whatever else in this account uses GitHub Actions, and declaring it here would
# make this module the owner of something it did not create.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

variable "accept_repository_subject_change" {
  description = <<-EOT
    False trusts both the default subject, repo:<repo>:environment:<env>,
    which main's Ship presents today, and the subject deploy/terraform/images
    switches the repository to, which also names the workflow file and ref.
    Set true once that switch is applied, to trust only Deploy on main.
  EOT
  type        = bool
  default     = false
}

locals {
  deploy_subjects = concat(
    var.accept_repository_subject_change ? [] : ["repo:${var.github_repository}:environment:${var.github_environment}"],
    [join(":", [
      "repo:${var.github_repository}",
      "environment:${var.github_environment}",
      "job_workflow_ref:${var.github_repository}/.github/workflows/deploy.yml@refs/heads/main",
      "ref:refs/heads/main",
    ])],
  )
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
          # Scoped to the environment as well as the repository, so a pull
          # request from a fork cannot reach the deployment and a deploy to
          # staging cannot write prod's values.
          "token.actions.githubusercontent.com:sub" = local.deploy_subjects
        }
      }
    }]
  })
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid     = "ReadDeploymentDescriptor"
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.storage["deploy"].arn}/${var.deployment}/values.json",
      # The reference platform's descriptor, for main's Deploy.
      "${aws_s3_bucket.storage["deploy"].arn}/${var.deployment}/infrastructure.json",
    ]
  }

  # Deploy checks that every image of the version it records exists.
  statement {
    sid       = "ReadReleaseImages"
    actions   = ["ecr:DescribeImages"]
    resources = ["${local.arn_prefix}:ecr:${var.region}:${local.account_id}:repository/${data.terraform_remote_state.core.outputs.ecr_repository_prefix}/release/*"]
  }

  statement {
    sid       = "AuthenticateToRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # main's Ship publishes the reference platform's images and its worker
  # image with this role.
  statement {
    sid       = "AuthenticateToReleaseRegistry"
    actions   = ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushReferenceImages"
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

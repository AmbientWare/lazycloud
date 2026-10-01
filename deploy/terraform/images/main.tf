# Image repositories and the release identity for the server, scheduler and
# agent images. Only what the existing platform roots lack: platform-core
# already owns <name>/scheduler, the EKS cluster, its OIDC provider and the
# workload image repository; platform-deployment owns each deployment's
# database, buckets, secrets and Pod Identity roles.

resource "aws_ecr_repository" "image" {
  for_each = toset(["server", "agent"])

  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.destroy_repositories_with_images

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Shared with the existing scheduler image; release tags (v*) and the
# existing commit tags do not collide, and tags are immutable.
data "aws_ecr_repository" "scheduler" {
  name = "${var.name}/scheduler"
}

# Untagged manifests accumulate as layers are rebuilt. Tagged images stay: a
# deployment names its tag and a rollback names an older one.
resource "aws_ecr_lifecycle_policy" "image" {
  for_each = aws_ecr_repository.image

  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 14 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 14
      }
      action = { type = "expire" }
    }]
  })
}

# Account-wide and created outside this root, like the deploy role's.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "release" {
  name        = "${var.name}-image-release"
  description = "GitHub Actions identity that pushes ${var.name} server, scheduler and agent images."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          # Only jobs in the release environment; pull requests and forks get
          # another subject.
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.github_environment}"
        }
      }
    }]
  })
}

data "aws_iam_policy_document" "release" {
  statement {
    sid       = "AuthenticateToRegistry"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "PushReleaseImages"
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
    resources = concat(
      [for repository in aws_ecr_repository.image : repository.arn],
      [data.aws_ecr_repository.scheduler.arn],
    )
  }
}

resource "aws_iam_role_policy" "release" {
  name   = "push-images"
  role   = aws_iam_role.release.name
  policy = data.aws_iam_policy_document.release.json
}

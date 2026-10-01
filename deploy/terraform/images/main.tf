# Image repositories and the release identity for the server, scheduler and
# agent images. Only what the existing platform roots lack: platform-core
# owns the EKS cluster, its OIDC provider and the workload image repository,
# and platform-deployment owns each deployment's database, buckets, secrets
# and Pod Identity roles. The images live under <name>/release/, apart from
# the reference platform's <name>/<image> repositories, so a reference
# deployment can never select one by tag.

locals {
  github_owner      = split("/", var.github_repository)[0]
  github_repository = split("/", var.github_repository)[1]
  environment       = "images"
  # Release tags have their own prefix: the reference Ship workflow cuts v*.
  tag_pattern = "platform-v*"
  workflow    = "${var.github_repository}/.github/workflows/release.yml"
}

resource "aws_ecr_repository" "image" {
  for_each = toset(["server", "scheduler", "agent"])

  name                 = "${var.name}/release/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.destroy_repositories_with_images

  image_scanning_configuration {
    scan_on_push = true
  }
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

# The GitHub side comes first, and the role depends on it, so the role never
# trusts a job the environment's protections have not gated.

# Release jobs wait for a reviewer and run only for platform-v* tags.
resource "github_repository_environment" "images" {
  repository  = local.github_repository
  environment = local.environment

  reviewers {
    users = var.release_reviewer_user_ids
  }
  prevent_self_review = false

  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "release_tags" {
  repository  = local.github_repository
  environment = github_repository_environment.images.environment
  tag_pattern = local.tag_pattern
}

# Only repository administrators create, move or delete release tags.
resource "github_repository_ruleset" "release_tags" {
  name        = "platform release tags"
  repository  = local.github_repository
  target      = "tag"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/tags/${local.tag_pattern}"]
      exclude = []
    }
  }

  # Repository role 5 is admin.
  bypass_actors {
    actor_id    = 5
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  rules {
    creation         = true
    update           = true
    deletion         = true
    non_fast_forward = true
  }
}

# Subjects then read
#   repo:<owner>/<repo>:environment:<env>:job_workflow_ref:<workflow>@<ref>:ref:<ref>
# for every workflow in the repository; see accept_repository_subject_change.
resource "github_actions_repository_oidc_subject_claim_customization_template" "repository" {
  repository         = local.github_repository
  use_default        = false
  include_claim_keys = ["repo", "context", "job_workflow_ref", "ref"]
}

# Account-wide and created outside this root, like the deploy role's.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "release" {
  name        = "${var.name}-image-release"
  description = "GitHub Actions identity that pushes ${var.name} server, scheduler and agent images."

  # Only release.yml, run for a platform-v* tag, in the images environment.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = join(":", [
            "repo:${var.github_repository}",
            "environment:${local.environment}",
            "job_workflow_ref:${local.workflow}@refs/tags/${local.tag_pattern}",
            "ref:refs/tags/${local.tag_pattern}",
          ])
        }
      }
    }]
  })

  depends_on = [
    github_repository_environment_deployment_policy.release_tags,
    github_repository_ruleset.release_tags,
    github_actions_repository_oidc_subject_claim_customization_template.repository,
  ]
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
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }
}

resource "aws_iam_role_policy" "release" {
  name   = "push-images"
  role   = aws_iam_role.release.name
  policy = data.aws_iam_policy_document.release.json
}

# The workflow reads these from the environment it runs in.
resource "github_actions_environment_variable" "release" {
  for_each = {
    AWS_IMAGE_RELEASE_ROLE_ARN = aws_iam_role.release.arn
    IMAGE_REGISTRY             = "${split("/", aws_ecr_repository.image["server"].repository_url)[0]}/${var.name}/release"
    AWS_REGION                 = var.region
  }

  repository    = local.github_repository
  environment   = github_repository_environment.images.environment
  variable_name = each.key
  value         = each.value
}

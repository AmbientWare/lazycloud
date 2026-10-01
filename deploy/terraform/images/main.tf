# Release identities: the repositories Ship pushes the server, scheduler and
# web images to, the role it pushes with, the role the Node images workflow
# bakes AMIs with, and the GitHub side that gates both. platform-core owns
# the cluster and the reference platform's <name>/<image> repositories;
# these images live under <name>/release/, so a reference deployment can
# never select one by tag.

locals {
  github_owner      = split("/", var.github_repository)[0]
  github_repository = split("/", var.github_repository)[1]
  environment       = "images"
  # Ship cuts v<version> tags and runs only from main.
  tag_pattern = "v*"
  ship        = "${var.github_repository}/.github/workflows/ship.yml"
  node_images = "${var.github_repository}/.github/workflows/node-images.yml"
}

resource "aws_ecr_repository" "image" {
  for_each = toset(["server", "scheduler", "web"])

  name                 = "${var.name}/release/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.destroy_repositories_with_images

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Untagged manifests accumulate as layers are rebuilt. Tagged images stay: a
# deployment names its version and a rollback names an older one.
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

# The GitHub side comes first, and the roles depend on it, so a role never
# trusts a job the environment's protections have not gated.

# Release jobs wait for a reviewer and run only from main.
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

resource "github_repository_environment_deployment_policy" "main" {
  repository     = local.github_repository
  environment    = github_repository_environment.images.environment
  branch_pattern = "main"
}

# Only repository administrators and Ship's workflow token create, move or
# delete release tags.
resource "github_repository_ruleset" "release_tags" {
  name        = "release tags"
  repository  = local.github_repository
  target      = "tag"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/tags/${local.tag_pattern}"]
      exclude = []
    }
  }

  # Repository role 5 is admin; integration 15368 is GitHub Actions.
  bypass_actors {
    actor_id    = 5
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  bypass_actors {
    actor_id    = 15368
    actor_type  = "Integration"
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

# Only Ship, run from main, in the images environment.
resource "aws_iam_role" "release" {
  name        = "${var.name}-image-release"
  description = "GitHub Actions identity that pushes ${var.name} server, scheduler and web images."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = join(":", [
            "repo:${var.github_repository}",
            "environment:${local.environment}",
            "job_workflow_ref:${local.ship}@refs/heads/main",
            "ref:refs/heads/main",
          ])
        }
      }
    }]
  })

  depends_on = [
    github_repository_environment_deployment_policy.main,
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

# Only the Node images workflow, run from main, in the images environment.
# It launches one tagged bake instance in the first region, images it and
# copies the image to the other regions.
resource "aws_iam_role" "node_images" {
  name        = "${var.name}-node-image-bake"
  description = "GitHub Actions identity that bakes ${var.name} fleet node images."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = join(":", [
            "repo:${var.github_repository}",
            "environment:${local.environment}",
            "job_workflow_ref:${local.node_images}@refs/heads/main",
            "ref:refs/heads/main",
          ])
        }
      }
    }]
  })

  depends_on = [
    github_repository_environment_deployment_policy.main,
    github_repository_ruleset.release_tags,
    github_actions_repository_oidc_subject_claim_customization_template.repository,
  ]
}

data "aws_iam_policy_document" "node_images" {
  statement {
    sid = "Inspect"
    actions = [
      "ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups", "ec2:DescribeRouteTables", "ec2:DescribeSnapshots",
      "ssm:GetParameters",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "LaunchTaggedBakeInstances"
    actions   = ["ec2:RunInstances", "ec2:CreateTags"]
    resources = ["arn:aws:ec2:*:*:instance/*", "arn:aws:ec2:*:*:volume/*", "arn:aws:ec2:*:*:network-interface/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/lazycloud:node-image-bake"
      values   = ["true"]
    }
  }

  statement {
    sid       = "LaunchInFleetNetworks"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:*:*:subnet/*", "arn:aws:ec2:*:*:security-group/*", "arn:aws:ec2:*::image/*"]
  }

  statement {
    sid       = "ManageBakeInstances"
    actions   = ["ec2:GetConsoleOutput", "ec2:StopInstances", "ec2:TerminateInstances", "ec2:CreateImage"]
    resources = ["arn:aws:ec2:*:*:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/lazycloud:node-image-bake"
      values   = ["true"]
    }
  }

  statement {
    sid       = "RegisterAndCopyImages"
    actions   = ["ec2:CreateImage", "ec2:CopyImage", "ec2:CreateTags"]
    resources = ["arn:aws:ec2:*::image/*", "arn:aws:ec2:*::snapshot/*"]
  }
}

resource "aws_iam_role_policy" "node_images" {
  name   = "bake-node-images"
  role   = aws_iam_role.node_images.name
  policy = data.aws_iam_policy_document.node_images.json
}

# The workflows read these from the environment they run in.
resource "github_actions_environment_variable" "release" {
  for_each = {
    AWS_IMAGE_RELEASE_ROLE_ARN = aws_iam_role.release.arn
    AWS_NODE_IMAGE_ROLE_ARN    = aws_iam_role.node_images.arn
    IMAGE_REGISTRY             = "${split("/", aws_ecr_repository.image["server"].repository_url)[0]}/${var.name}/release"
    AWS_REGION                 = var.region
  }

  repository    = local.github_repository
  environment   = github_repository_environment.images.environment
  variable_name = each.key
  value         = each.value
}

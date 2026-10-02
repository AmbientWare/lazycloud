# The repository's release machinery: its environments and their
# protections, the v* tag rule, the OIDC subject template, and the AWS roles
# Ship and Node images run as. Apply after platform-core and
# platform-deployment, with a repository administrator's GITHUB_TOKEN.

data "terraform_remote_state" "core" {
  backend = "s3"
  config  = merge(jsondecode(file(var.terraform_backend_config)), { key = "platform-core/lazycloud.tfstate" })
}

data "terraform_remote_state" "deployment" {
  backend = "s3"
  config  = merge(jsondecode(file(var.terraform_backend_config)), { key = "platform-deployment/${var.deployment}.tfstate" })
}

locals {
  repository = split("/", var.github_repository)[1]
  core       = data.terraform_remote_state.core.outputs
  # Every environment admits jobs from main only, and the workflows that use
  # them refuse anyone but a repository admin; release publishes to PyPI.
  environments = toset(["images", "release", "prod"])
  environment_variables = {
    images  = { AWS_IMAGE_RELEASE_ROLE_ARN = aws_iam_role.workflow["ship"].arn, AWS_NODE_IMAGE_ROLE_ARN = aws_iam_role.workflow["node-images"].arn, IMAGE_REGISTRY = local.core.release_registry }
    release = {}
    prod    = { AWS_DEPLOY_ROLE_ARN = data.terraform_remote_state.deployment.outputs.deploy_role_arn }
  }
  variables = merge([for environment, values in local.environment_variables : {
    for name, value in merge(values, { AWS_REGION = local.core.region }) : "${environment}/${name}" => { environment = environment, name = name, value = value }
  }]...)
}

resource "github_repository_environment" "environment" {
  for_each    = local.environments
  repository  = local.repository
  environment = each.key

  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "main" {
  for_each       = github_repository_environment.environment
  repository     = local.repository
  environment    = each.value.environment
  branch_pattern = "main"
}

resource "github_actions_environment_variable" "environment" {
  for_each      = local.variables
  repository    = local.repository
  environment   = github_repository_environment.environment[each.value.environment].environment
  variable_name = each.value.name
  value         = each.value.value
}

# Release tags: Ship's version job creates v* tags with the workflow token,
# and only administrators (repository role 5) move or delete one. Deploy
# refuses a tag whose commit is not on main, so a tag made elsewhere cannot
# reach prod. GitHub does not accept the Actions integration as a bypass on
# a repository ruleset, so creation itself stays open to writers.
resource "github_repository_ruleset" "release_tags" {
  name        = "release tags"
  repository  = local.repository
  target      = "tag"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/tags/v*"]
      exclude = []
    }
  }

  bypass_actors {
    actor_id    = 5
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  rules {
    update           = true
    deletion         = true
    non_fast_forward = true
  }
}

# prod is what Argo CD runs. Only Deploy pushes it, with this deploy key,
# which lives in the prod environment and so only in jobs from main that
# the environment admits; no workflow token and no person but an
# administrator can push it.
resource "tls_private_key" "prod_deploy" {
  algorithm = "ED25519"
}

resource "github_repository_deploy_key" "prod_deploy" {
  repository = local.repository
  title      = "Deploy: prod branch"
  key        = tls_private_key.prod_deploy.public_key_openssh
  read_only  = false
}

resource "github_actions_environment_secret" "prod_deploy_key" {
  repository      = local.repository
  environment     = github_repository_environment.environment["prod"].environment
  secret_name     = "PROD_DEPLOY_KEY"
  plaintext_value = tls_private_key.prod_deploy.private_key_openssh
}

resource "github_repository_ruleset" "prod" {
  name        = "prod branch"
  repository  = local.repository
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/heads/prod"]
      exclude = []
    }
  }

  bypass_actors {
    actor_id    = 5
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  bypass_actors {
    actor_type  = "DeployKey"
    bypass_mode = "always"
  }

  rules {
    creation         = true
    update           = true
    deletion         = true
    non_fast_forward = true
  }
}

# Subjects read repo:<repo>:environment:<env>:job_workflow_ref:<repo>/.github/workflows/<file>@<ref>,
# so each AWS role trusts one workflow file on main in one environment.
resource "github_actions_repository_oidc_subject_claim_customization_template" "repository" {
  repository         = local.repository
  use_default        = false
  include_claim_keys = ["repo", "context", "job_workflow_ref"]
}

data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "workflow" {
  for_each = toset(["ship", "node-images"])
  name     = "${var.name}-${each.key}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:images:job_workflow_ref:${var.github_repository}/.github/workflows/${each.key}.yml@refs/heads/main"
      } }
    }]
  })
  depends_on = [github_repository_environment_deployment_policy.main, github_actions_repository_oidc_subject_claim_customization_template.repository]
}

# Ship pushes the release images.
resource "aws_iam_role_policy" "ship" {
  name = "push-release-images"
  role = aws_iam_role.workflow["ship"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"]
        Resource = values(local.core.release_repositories)
      },
    ]
  })
}

# Node images launches one tagged bake instance, images it once it has
# stopped and copies the image to the other fleet regions.
data "aws_iam_policy_document" "node_images" {
  statement {
    actions   = ["ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ssm:GetParameters"]
    resources = ["*"]
  }

  statement {
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:*:*:instance/*", "arn:aws:ec2:*:*:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/lazycloud:node-image-bake"
      values   = ["true"]
    }
  }

  # Tags only what a launch creates, so no other instance can be tagged
  # into the bake's terminate rights.
  statement {
    actions   = ["ec2:CreateTags"]
    resources = ["arn:aws:ec2:*:*:instance/*", "arn:aws:ec2:*:*:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  statement {
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:*:*:subnet/*", "arn:aws:ec2:*:*:security-group/*", "arn:aws:ec2:*:*:network-interface/*", "arn:aws:ec2:*::image/*"]
  }

  statement {
    actions   = ["ec2:GetConsoleOutput", "ec2:StopInstances", "ec2:TerminateInstances", "ec2:CreateImage"]
    resources = ["arn:aws:ec2:*:*:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/lazycloud:node-image-bake"
      values   = ["true"]
    }
  }

  statement {
    actions   = ["ec2:CreateImage", "ec2:CopyImage"]
    resources = ["arn:aws:ec2:*::image/*", "arn:aws:ec2:*::snapshot/*"]
  }

  statement {
    actions   = ["ec2:CreateTags"]
    resources = ["arn:aws:ec2:*::image/*", "arn:aws:ec2:*::snapshot/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["CreateImage", "CopyImage"]
    }
  }
}

resource "aws_iam_role_policy" "node_images" {
  name   = "bake-node-images"
  role   = aws_iam_role.workflow["node-images"].id
  policy = data.aws_iam_policy_document.node_images.json
}

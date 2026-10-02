# The server and scheduler pods' identity, through Pod Identity. It is the
# principal customer connection roles trust (LAZYCLOUD_AWS_PRINCIPAL_ARN),
# so its name is an external contract once a customer connects. It holds
# what the binaries call: S3 for objects and workspace buckets, STS for
# host storage grants and customer connections, ECR for workload images,
# EC2 for the fleet and for sharing node images with connected accounts.
resource "aws_iam_role" "control_plane" {
  name = "${var.deployment}-control-plane"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "pods.eks.amazonaws.com" }, Action = ["sts:AssumeRole", "sts:TagSession"] }]
  })
}

# Session tags off: tags on a Pod Identity session are transitive, so every
# role assumed onward would have to allow sts:TagSession, and customer
# connection roles allow sts:AssumeRole alone.
resource "aws_eks_pod_identity_association" "control_plane" {
  for_each             = toset(["lazycloud-server", "lazycloud-scheduler"])
  cluster_name         = local.core.cluster_name
  namespace            = var.deployment
  service_account      = each.value
  role_arn             = aws_iam_role.control_plane.arn
  disable_session_tags = true
}

locals {
  objects_arn  = aws_s3_bucket.storage["objects"].arn
  workload_arn = "${local.arn_prefix}:ecr:${var.region}:${local.account_id}:repository/${local.core.workload_image_repository}/*"
  ec2_arn      = "${local.arn_prefix}:ec2:*:${local.account_id}"
}

data "aws_iam_policy_document" "control_plane" {
  statement {
    sid       = "Objects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = ["${local.objects_arn}/*", "${local.workspace_bucket_arn}/*"]
  }

  # The server sets the dashboard's CORS rule on every bucket it presigns
  # for, and creates each workspace bucket with its lifecycle rule.
  statement {
    sid       = "Buckets"
    actions   = ["s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutBucketCORS"]
    resources = [local.objects_arn, local.workspace_bucket_arn]
  }

  statement {
    sid       = "CreateWorkspaceBuckets"
    actions   = ["s3:CreateBucket", "s3:PutLifecycleConfiguration"]
    resources = [local.workspace_bucket_arn]
  }

  statement {
    sid       = "HostStorageGrants"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.workspace_storage.arn]
  }

  # Customer connection roles, with their external id. Not narrowed by name:
  # an existing-role connection names any role, and validation proves the
  # customer's side enforces the external id.
  statement {
    sid       = "CustomerConnections"
    actions   = ["sts:AssumeRole"]
    resources = ["${local.arn_prefix}:iam::*:role/*"]
  }

  # The server's own login pins base images and checks what builds pushed.
  # Hosts never get it: their logins come from registry_hosts.
  statement {
    sid       = "RegistryLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadWorkloadImages"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [local.workload_arn]
  }

  # Fleet hosts: RunInstances with the host id as client token. The
  # launcher tags the instance, its volumes and a reserve's persistent Spot
  # request at launch and may tag nothing else; only tagged instances can be
  # started, stopped or terminated, and only tagged requests cancelled.
  # RunInstances also creates the network interface and a serving host's
  # one-time Spot request, which carry no tag.
  statement {
    sid = "DescribeFleet"
    actions = [
      "ec2:DescribeInstances", "ec2:DescribeSubnets", "ec2:DescribeSpotInstanceRequests", "ec2:DescribeSpotPriceHistory",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "LaunchTagged"
    actions   = ["ec2:RunInstances"]
    resources = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/lazycloud:fleet"
      values   = [local.fleet_name]
    }
  }

  statement {
    sid       = "TagAtLaunch"
    actions   = ["ec2:CreateTags"]
    resources = ["${local.ec2_arn}:instance/*", "${local.ec2_arn}:volume/*", "${local.ec2_arn}:spot-instances-request/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  # Connected accounts launch the baked node images: before each launch the
  # launcher grants the account launch permission on the image it uses.
  # Image ARNs carry no account, so the node-image tag the bake sets
  # scopes this to the platform's own images.
  statement {
    sid       = "ShareNodeImages"
    actions   = ["ec2:ModifyImageAttribute"]
    resources = ["${local.arn_prefix}:ec2:*::image/*"]
    condition {
      test     = "Null"
      variable = "aws:ResourceTag/lazycloud:node-image"
      values   = ["false"]
    }
  }

  statement {
    sid     = "LaunchInFleetNetworks"
    actions = ["ec2:RunInstances"]
    resources = [
      "${local.ec2_arn}:subnet/*", "${local.ec2_arn}:security-group/*", "${local.ec2_arn}:network-interface/*",
      "${local.ec2_arn}:spot-instances-request/*", "${local.arn_prefix}:ec2:*::image/*",
    ]
  }

  # Reserves stop, hibernate and start in place.
  statement {
    sid       = "ActOnTagged"
    actions   = ["ec2:TerminateInstances", "ec2:StartInstances", "ec2:StopInstances"]
    resources = ["${local.ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/lazycloud:fleet"
      values   = [local.fleet_name]
    }
  }

  # A Spot reserve's persistent request is cancelled before its instance
  # terminates, or EC2 launches a replacement.
  statement {
    sid       = "CancelTaggedSpotRequests"
    actions   = ["ec2:CancelSpotInstanceRequests"]
    resources = ["${local.ec2_arn}:spot-instances-request/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/lazycloud:fleet"
      values   = [local.fleet_name]
    }
  }

  statement {
    sid       = "PassNodeRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.fleet_node.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  # The first Spot launch in an account creates Spot's service-linked role.
  statement {
    sid       = "SpotServiceRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["spot.amazonaws.com"]
    }
  }
}

# Hosts' registry logins (LAZYCLOUD_IMAGE_REGISTRY_HOST_ROLE_ARN). The
# server assumes this role for each host command with a session policy that
# names only that command's repositories: an image to pull, or a build's
# image and its workspace's cache, or a workspace's snapshots to push. This
# grant is the ceiling of those sessions. The control plane's
# CustomerConnections statement covers the AssumeRole.
resource "aws_iam_role" "registry_hosts" {
  name = "${var.deployment}-registry-hosts"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { AWS = aws_iam_role.control_plane.arn }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "registry_hosts" {
  name = "workload-images"
  role = aws_iam_role.registry_hosts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        # ECR creates each repository on its first push.
        Effect = "Allow"
        Action = [
          "ecr:CreateRepository", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = local.workload_arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "control-plane"
  role   = aws_iam_role.control_plane.name
  policy = data.aws_iam_policy_document.control_plane.json
}

# Fleet instances run as this role. They prove their identity to the server
# with a presigned GetCallerIdentity, which needs no permission; SSM lets an
# operator open a session on a host.
resource "aws_iam_role" "fleet_node" {
  name = "${var.deployment}-fleet-node"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "fleet_node_ssm" {
  role       = aws_iam_role.fleet_node.name
  policy_arn = "${local.arn_prefix}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "fleet_node" {
  name = aws_iam_role.fleet_node.name
  role = aws_iam_role.fleet_node.name
}

# External Secrets' store for this namespace exchanges the secrets-reader
# service account's token (IRSA) for this role, which reads only this
# deployment's documents.
resource "aws_iam_role" "secrets_reader" {
  name = "${var.deployment}-secrets-reader"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.core.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "${local.core.oidc_issuer_host}:aud" = "sts.amazonaws.com"
        "${local.core.oidc_issuer_host}:sub" = "system:serviceaccount:${var.deployment}:secrets-reader"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "secrets_reader" {
  name = "read-deployment-secrets"
  role = aws_iam_role.secrets_reader.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
      Resource = [aws_secretsmanager_secret.platform.arn, data.aws_secretsmanager_secret.operator.arn]
    }]
  })
}

# The Deploy workflow on main, in this deployment's GitHub environment. The
# github root sets the repository's subject template to name the workflow
# file. The account's GitHub OIDC provider exists outside these roots.
data "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"
}

resource "aws_iam_role" "deploy" {
  name = "${var.deployment}-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.github_repository}:environment:${var.github_environment}:job_workflow_ref:${var.github_repository}/.github/workflows/deploy.yml@refs/heads/main"
      } }
    }]
  })
}

# Deploy reads the chart values and checks the version's images exist.
resource "aws_iam_role_policy" "deploy" {
  name = "deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.storage["deploy"].arn}/${aws_s3_object.values.key}" },
      { Effect = "Allow", Action = "ecr:DescribeImages", Resource = values(local.core.release_repositories) },
    ]
  })
}

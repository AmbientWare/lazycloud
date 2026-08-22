data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  arn_prefix = "arn:${data.aws_partition.current.partition}"

  # Must match LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX in the deployment
  # environment. The control plane names buckets with it and this policy is what
  # permits them, so the two are one decision written twice; the runtime output
  # below is the same value, so a deployment cannot set them differently.
  workspace_bucket_prefix = "${var.deployment}-workspace"

  # The issuer host, which is what an IRSA condition is written against. The
  # scheme is stripped because a condition key names the provider by host.
  oidc_subject_key = replace(aws_iam_openid_connect_provider.cluster.url, "https://", "")
}

# The identity the control plane and scheduler pods run as. It is also the
# principal the connected-AWS control role trusts, so its ARN belongs in the
# control stack's TrustedPrincipalArns.
#
# Assumed through the cluster's OIDC provider rather than carried by a node, so
# the credential belongs to the workload rather than to whatever else happens to
# be scheduled beside it. The subject names the service accounts exactly: a
# wildcard here would let any pod in the namespace assume the role that reaches
# every workspace bucket.
resource "aws_iam_role" "control_plane" {
  name        = "${var.deployment}-control-plane"
  description = "Identity of the LazyCloud control plane and scheduler workloads."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.cluster.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_subject_key}:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "${local.oidc_subject_key}:sub" = [
            for name in var.control_plane_service_accounts :
            "system:serviceaccount:${var.kubernetes_namespace}:${name}"
          ]
        }
      }
    }]
  })
}

data "aws_iam_policy_document" "control_plane" {
  statement {
    sid       = "PullControlPlaneImages"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid = "ReadOwnRepositories"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [for repository in aws_ecr_repository.image : repository.arn]
  }

  statement {
    sid       = "ReadDeploymentBundle"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"]
    resources = [aws_s3_bucket.deploy.arn, "${aws_s3_bucket.deploy.arn}/*"]
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [for secret in aws_secretsmanager_secret.runtime : secret.arn]
  }

  # A bucket per workspace, created on demand by `control.service.create_workspace_storage`.
  # Scoped by prefix rather than enumerated, because the set is not known until
  # customers exist — and scoped by *this deployment's* prefix, because two
  # deployments in one account share the bucket namespace and a grant on a bare
  # `workspace-*` would reach the other one's customers.
  statement {
    sid = "OwnPlatformAndWorkspaceBuckets"
    actions = [
      "s3:CreateBucket",
      "s3:DeleteObject",
      "s3:GetBucketLocation",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:ListBucketMultipartUploads",
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = concat(
      [for bucket in aws_s3_bucket.objects : bucket.arn],
      [for bucket in aws_s3_bucket.objects : "${bucket.arn}/*"],
      [
        "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*",
        "${local.arn_prefix}:s3:::${local.workspace_bucket_prefix}-*/*",
      ],
    )
  }
}

resource "aws_iam_role_policy" "control_plane" {
  name   = "control-plane"
  role   = aws_iam_role.control_plane.name
  policy = data.aws_iam_policy_document.control_plane.json
}

# The identity the control plane assumes to reach a connected account.
#
# This lives in our account, so it is declared here rather than deployed as a
# CloudFormation stack. It was a stack only because the script that created it
# also generates customer templates, which is an implementation accident.
#
# The name is pinned and must stay pinned. A customer's authorization template
# writes this role's ARN into every connection role's trust policy, so the name
# is a durable external contract rather than a label. Terraform likes generated
# and suffixed names; this one takes neither.
resource "aws_iam_role" "control_principal" {
  name        = var.control_role_name
  description = "LazyCloud platform control principal for connected AWS."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.control_plane.arn }
      # Both, because the workload reaches this through Pod Identity and such a
      # session carries tags. Propagating them is `sts:TagSession`, and a trust
      # granting only the assume refuses the call in terms of the tagging.
      Action = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })

}

# Deleting this role is irreversible for a customer who has already connected: AWS
# rewrites a role-ARN principal in their trust policy to this role's unique ID,
# and recreating the same name does not restore that trust. Every live connection
# would need a new authorization generation.
#
# There is no `prevent_destroy` here, deliberately. It takes no variable, so it
# cannot be "off while predeployment and on afterwards", and a guard that cannot
# be switched is one people work around instead of thinking about. Add it in this
# file the day the first customer connects; until then the deployment has to be
# destroyable to be provably rebuildable.

data "aws_iam_policy_document" "control_principal" {
  # Every EC2, Auto Scaling and IAM call in the connection and capacity lifecycle
  # runs inside an assumed customer session, so this is the platform's only broad
  # grant.
  #
  # Deliberately not scoped to role/compute-connection-*, and deliberately
  # carrying no sts:ExternalId condition. The public connection contract lets a
  # customer name an arbitrary role for existing-role mode, and the external-ID
  # enforcement probe proves the customer enforces it by expecting AccessDenied
  # from their side. A platform-side condition would deny that call first and make
  # the probe pass without proving anything.
  statement {
    sid       = "AssumeCustomerConnectionRoles"
    actions   = ["sts:AssumeRole"]
    resources = ["${local.arn_prefix}:iam::*:role/*"]
  }

  # No account-scoped deny sits under that wildcard. A customer account can be
  # this account: acceptance connects the platform account to itself, and so does
  # the shared fleet. Denying assumption of local roles would block the connection
  # role the control plane must assume.

  # Image describes do not support resource-level authorization.
  statement {
    sid       = "InspectCapacityImages"
    actions   = ["ec2:DescribeImages"]
    resources = ["*"]
  }

  statement {
    sid       = "ShareOwnedCapacityImages"
    actions   = ["ec2:ModifyImageAttribute"]
    resources = ["${local.arn_prefix}:ec2:*::image/*"]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/cloud-pool:managed-by"
      values   = ["control-plane"]
    }
  }
}

resource "aws_iam_role_policy" "control_principal" {
  name   = "connected-aws-control"
  role   = aws_iam_role.control_principal.name
  policy = data.aws_iam_policy_document.control_principal.json
}

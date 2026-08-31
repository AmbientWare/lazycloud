# Roles the live acceptance scenarios run as. Only an account used for those
# needs them, which is why they are gated rather than always created; they are
# not a code path, they are infrastructure that exists in one account and not
# another.
#
# The separation between them is the point. The operator role drives scenarios
# and can read what went wrong, but holds no IAM write authority of its own:
# CloudFormation performs those writes through the execution role, and the
# operator can only pass it. Node diagnostics are separate again, because running
# a command on a node is remote code execution and a scenario should not carry
# that for its whole duration.

resource "aws_iam_role" "customer_stack_execution" {
  count = var.create_acceptance_roles ? 1 : 0

  name        = "${var.deployment}-customer-connection-stack"
  description = "CloudFormation service role for LazyCloud customer connection stacks."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudformation.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "customer_stack_execution" {
  count = var.create_acceptance_roles ? 1 : 0

  name = "customer-connection-stack"
  role = aws_iam_role.customer_stack_execution[0].name

  # The same document `customer_stack.py` documents as the execution policy. One
  # file, referenced, rather than a transcription that can disagree with it.
  policy = file("${path.module}/../connected-aws/customer-stack-execution-policy.json")
}

resource "aws_iam_role" "acceptance_operator" {
  count = var.create_acceptance_roles ? 1 : 0

  name        = "${var.deployment}-acceptance-operator"
  description = "LazyCloud connected AWS acceptance operator."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.acceptance_trusted_principal_arns }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "acceptance_operator" {
  # Scenario corroboration filters by tag at the query, but EC2 and Auto Scaling
  # describes do not support resource-level authorization.
  statement {
    sid = "CorroborateScopedCapacity"
    actions = [
      "autoscaling:DescribeAutoScalingGroups",
      "ec2:DescribeInstances",
      "ec2:DescribeVolumes",
    ]
    resources = ["*"]
  }

  # Enough to diagnose a failed run without escalating. Mutation stays with the
  # execution role.
  statement {
    sid = "InspectStacks"
    actions = [
      "cloudformation:DescribeStackEvents",
      "cloudformation:DescribeStackResources",
      "cloudformation:DescribeStacks",
      "cloudformation:ListStacks",
      "cloudformation:ValidateTemplate",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ManageCustomerConnectionStacks"
    actions   = ["cloudformation:CreateStack", "cloudformation:DeleteStack"]
    resources = ["${local.arn_prefix}:cloudformation:*:${data.aws_caller_identity.current.account_id}:stack/compute-connection-*"]
  }

  statement {
    sid       = "PassCustomerStackExecutionRole"
    actions   = ["iam:PassRole"]
    resources = var.create_acceptance_roles ? [aws_iam_role.customer_stack_execution[0].arn] : []

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["cloudformation.amazonaws.com"]
    }
  }

  # The acceptance profile chain hops through this role into the control role.
  statement {
    sid       = "AssumeControlPrincipal"
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.control_principal.arn]
  }
}

resource "aws_iam_role_policy" "acceptance_operator" {
  count = var.create_acceptance_roles ? 1 : 0

  name   = "connected-aws-acceptance"
  role   = aws_iam_role.acceptance_operator[0].name
  policy = data.aws_iam_policy_document.acceptance_operator.json
}

# A node that fails after handoff is otherwise unreachable: no public ingress,
# no inbound administrative transport, and cloud-init output going nowhere a caller can
# read. Every diagnosis so far cost a full launch cycle for want of these four
# calls. Break-glass rather than operator permissions, because SendCommand is
# remote code execution on a running node.
resource "aws_iam_role" "node_diagnostics" {
  count = var.create_acceptance_roles ? 1 : 0

  name        = "${var.deployment}-node-diagnostics"
  description = "LazyCloud break-glass node diagnostics."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = var.acceptance_trusted_principal_arns }
      Action    = "sts:AssumeRole"
    }]
  })
}

data "aws_iam_policy_document" "node_diagnostics" {
  # Reads only. SSM does not support resource-level authorization on these, so
  # the write below is where the scoping has to hold.
  statement {
    sid = "InspectManagedNodes"
    actions = [
      "ssm:DescribeInstanceInformation",
      "ssm:GetCommandInvocation",
      "ssm:ListCommandInvocations",
      "ssm:ListCommands",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "UseShellDocument"
    actions   = ["ssm:SendCommand"]
    resources = ["${local.arn_prefix}:ssm:*::document/AWS-RunShellScript"]
  }

  # The tag is applied at launch by the managed pool, so a node this platform did
  # not start cannot be targeted.
  statement {
    sid       = "TargetOnlyOwnedNodes"
    actions   = ["ssm:SendCommand"]
    resources = ["${local.arn_prefix}:ec2:*:*:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ssm:resourceTag/cloud-pool:managed-by"
      values   = ["control-plane"]
    }
  }

  # The only diagnostic that survives a node which never reached userland, where
  # SSM cannot help. Treat its output as sensitive: kernel messages can carry
  # instance metadata.
  statement {
    sid       = "ReadNodeConsole"
    actions   = ["ec2:GetConsoleOutput"]
    resources = ["${local.arn_prefix}:ec2:*:*:instance/*"]
  }
}

resource "aws_iam_role_policy" "node_diagnostics" {
  count = var.create_acceptance_roles ? 1 : 0

  name   = "node-diagnostics"
  role   = aws_iam_role.node_diagnostics[0].name
  policy = data.aws_iam_policy_document.node_diagnostics.json
}

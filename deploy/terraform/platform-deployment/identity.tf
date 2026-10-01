# Pod Identity: which service accounts in this deployment's namespace hold
# the control plane role. An association names a cluster, a namespace and a
# service account beside the cluster, so `lazycloud-staging/lazycloud-server`
# never holds prod's role.
#
# The platform's associations disable session tags. Tags on a Pod Identity
# session are transitive, so every role it assumes onward would have to
# allow sts:TagSession; customer connection roles allow sts:AssumeRole only
# (internal/compute/connection_template.json). Nothing reads the tags.
#
# The reference chart's service accounts keep their associations until
# main's deployment is retired.

resource "aws_eks_pod_identity_association" "control_plane" {
  for_each = toset(values(var.control_plane_service_accounts))

  cluster_name    = local.cluster_name
  namespace       = var.deployment
  service_account = each.value
  role_arn        = aws_iam_role.control_plane.arn
}

resource "aws_eks_pod_identity_association" "platform" {
  for_each = var.platform_service_accounts

  cluster_name         = local.cluster_name
  namespace            = var.deployment
  service_account      = each.value
  role_arn             = aws_iam_role.control_plane.arn
  disable_session_tags = true
}

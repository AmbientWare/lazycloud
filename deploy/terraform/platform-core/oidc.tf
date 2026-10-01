# The federation one identity per deployment uses.
#
# Workload pods hold their AWS identity through Pod Identity, which names a
# cluster, a namespace and a service account beside the cluster. The External
# Secrets store cannot: it is reconciled by an operator shared across every
# namespace, and the only per-store identity the operator offers is a service
# account token exchanged through IRSA. This provider is what that exchange
# trusts, and a deployment's `secrets-reader` role conditions on the subject
# `system:serviceaccount:<deployment>:secrets-reader`, so a store in one
# namespace cannot present another's identity.
#
# No thumbprint. AWS validates EKS issuers against its own trust store, so there
# is no certificate here to keep current.
resource "aws_iam_openid_connect_provider" "cluster" {
  url            = aws_eks_cluster.control_plane.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]

  tags = { Name = var.name }
}

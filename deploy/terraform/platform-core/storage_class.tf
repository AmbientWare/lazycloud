# The class Auto Mode does not create for you.
#
# Auto Mode runs the EBS CSI driver but declares no StorageClass, and the
# cluster's only other one is a legacy `gp2` that is not marked default. A claim
# naming neither binds to nothing and reports "no storage class is set", which
# stalls a whole Argo sync: it waits for the claim to become healthy and every
# resource behind it waits with it.
#
# Declared here rather than in the chart because it is cluster-scoped. Two
# deployments syncing the same chart would each claim it, and Argo would prune
# it out from under the other.
resource "kubernetes_storage_class_v1" "ebs" {
  metadata {
    name = var.storage_class_name
  }

  storage_provisioner = "ebs.csi.eks.amazonaws.com"
  volume_binding_mode = "WaitForFirstConsumer"
  # A cache is rebuilt on a miss, so a volume that outlived its claim would be
  # paid for and never read again.
  reclaim_policy         = "Delete"
  allow_volume_expansion = true

  parameters = {
    type      = "gp3"
    encrypted = "true"
  }
}

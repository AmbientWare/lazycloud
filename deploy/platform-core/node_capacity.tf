locals {
  # Bootstrap capacity before Argo can run. These are infrastructure resources,
  # carried by the bootstrap release so a fresh plan needs no Kubernetes CRDs.
  node_capacity = [
    {
      apiVersion = "eks.amazonaws.com/v1"
      kind       = "NodeClass"
      metadata   = { name = "platform" }
      spec = {
        role                       = aws_iam_role.node.name
        subnetSelectorTerms        = [for subnet in aws_subnet.cluster : { id = subnet.id }]
        securityGroupSelectorTerms = [{ id = aws_eks_cluster.control_plane.vpc_config[0].cluster_security_group_id }]
        ephemeralStorage = {
          size       = "80Gi"
          iops       = 3000
          throughput = 125
        }
      }
    },
    {
      apiVersion = "karpenter.sh/v1"
      kind       = "NodePool"
      metadata   = { name = "platform-spot" }
      spec = {
        template = {
          spec = {
            nodeClassRef = {
              group = "eks.amazonaws.com"
              kind  = "NodeClass"
              name  = "platform"
            }
            requirements = [
              { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot"] },
              { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
              { key = "kubernetes.io/os", operator = "In", values = ["linux"] },
              { key = "eks.amazonaws.com/instance-category", operator = "In", values = ["c", "m", "r"] },
              { key = "eks.amazonaws.com/instance-generation", operator = "Gt", values = ["4"] },
            ]
            expireAfter            = "336h"
            terminationGracePeriod = "24h"
          }
        }
        disruption = {
          consolidationPolicy = "WhenEmptyOrUnderutilized"
          consolidateAfter    = "5m"
          budgets             = [{ nodes = "1" }]
        }
      }
    },
  ]
}

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_eks
from aws_cdk import aws_iam as iam
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws.eks_cluster import EksCluster


class KarpenterConstruct(Construct):
    """Deploy Karpenter autoscaler using Helm"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        vpc: ec2.Vpc,
        config: EnvironmentConfig,
        karpenter_controller_role: iam.CfnRole,
        karpenter_node_role: iam.CfnRole,
        karpenter_namespace: str = "kube-system",
    ) -> None:
        super().__init__(scope, construct_id)

        self.eks_cluster = eks_cluster
        self.vpc = vpc
        self.config = config
        self.karpenter_namespace = karpenter_namespace
        self.karpenter_controller_role = karpenter_controller_role
        self.karpenter_node_role = karpenter_node_role

        # Deploy Karpenter
        self.helm_chart = self._deploy_karpenter_helm()

        # Create default NodePool and EC2NodeClass
        # Note: VPC resources are already tagged in shared_infrastructure.py
        self._create_default_nodepool()

        # Create gVisor-enabled NodePool for isolated sandboxes
        self._create_gvisor_nodepool()

    def _get_helm_values(self) -> dict:
        """Get Helm values for Karpenter - matching official getting started guide"""
        values = {
            "settings": {
                "clusterName": self.eks_cluster.cluster_name,
                "clusterEndpoint": self.eks_cluster.cluster_endpoint,
                "interruptionQueue": f"{self.config.org_name}-{self.config.environment}-karpenter",
            },
            "serviceAccount": {
                "name": "karpenter",
                "annotations": {
                    "eks.amazonaws.com/role-arn": self.karpenter_controller_role.attr_arn,
                },
            },
            "controller": {
                "resources": {
                    "requests": {
                        "cpu": "1",
                        "memory": "1Gi",
                    },
                    "limits": {
                        "cpu": "1",
                        "memory": "1Gi",
                    },
                },
            },
        }

        return values

    def _deploy_karpenter_helm(self) -> aws_eks.HelmChart:
        """Deploy Karpenter using Helm chart"""
        helm_chart = aws_eks.HelmChart(
            self,
            "KarpenterHelmChart",
            cluster=self.eks_cluster.cluster,
            chart="karpenter",
            repository="oci://public.ecr.aws/karpenter/karpenter",
            namespace=self.karpenter_namespace,
            create_namespace=True,
            wait=True,
            timeout=cdk.Duration.minutes(
                15
            ),  # Increased timeout for better reliability
            values=self._get_helm_values(),
            version="1.7.2",
        )

        return helm_chart

    def _create_default_nodepool(self) -> None:
        """Create default NodePool and EC2NodeClass CRDs - following official docs"""

        # EC2NodeClass manifest - using tag-based discovery like official docs
        ec2_node_class_manifest = {
            "apiVersion": "karpenter.k8s.aws/v1",
            "kind": "EC2NodeClass",
            "metadata": {"name": "default"},
            "spec": {
                "role": self.karpenter_node_role.role_name,
                "amiSelectorTerms": [
                    {
                        "alias": "al2023@latest",
                    }
                ],
                "subnetSelectorTerms": [
                    {
                        "tags": {
                            "karpenter.sh/discovery": self.eks_cluster.cluster_name,
                        }
                    }
                ],
                "securityGroupSelectorTerms": [
                    {
                        "id": self.eks_cluster.cluster_security_group_id,
                    }
                ],
            },
        }

        # NodePool manifest - matching official docs
        node_pool_manifest = {
            "apiVersion": "karpenter.sh/v1",
            "kind": "NodePool",
            "metadata": {"name": "default"},
            "spec": {
                "template": {
                    "spec": {
                        "requirements": [
                            {
                                "key": "kubernetes.io/arch",
                                "operator": "In",
                                "values": ["amd64"],
                            },
                            {
                                "key": "kubernetes.io/os",
                                "operator": "In",
                                "values": ["linux"],
                            },
                            {
                                "key": "karpenter.sh/capacity-type",
                                "operator": "In",
                                "values": ["on-demand"],
                            },
                            {
                                "key": "karpenter.k8s.aws/instance-category",
                                "operator": "In",
                                "values": ["c", "m", "r"],
                            },
                            {
                                "key": "karpenter.k8s.aws/instance-generation",
                                "operator": "Gt",
                                "values": ["2"],
                            },
                        ],
                        "nodeClassRef": {
                            "group": "karpenter.k8s.aws",
                            "kind": "EC2NodeClass",
                            "name": "default",
                        },
                        "expireAfter": "720h",
                    },
                },
                "limits": {
                    "cpu": "1000",
                },
                "disruption": {
                    "consolidationPolicy": "WhenEmptyOrUnderutilized",
                    "consolidateAfter": "1m",
                },
            },
        }

        # Apply the manifests with dependency on Helm chart
        ec2_node_class = aws_eks.KubernetesManifest(
            self,
            "EC2NodeClass",
            cluster=self.eks_cluster.cluster,
            manifest=[ec2_node_class_manifest],
            prune=True,
            overwrite=True,
        )
        ec2_node_class.node.add_dependency(self.helm_chart)

        node_pool = aws_eks.KubernetesManifest(
            self,
            "NodePool",
            cluster=self.eks_cluster.cluster,
            manifest=[node_pool_manifest],
            prune=True,
            overwrite=True,
        )
        node_pool.node.add_dependency(self.helm_chart)
        node_pool.node.add_dependency(ec2_node_class)

    def _create_gvisor_nodepool(self) -> None:
        """Create gVisor-enabled NodePool and EC2NodeClass for isolated sandboxes"""

        # EC2NodeClass manifest for gVisor nodes - using tag-based discovery + gVisor setup
        gvisor_ec2_node_class_manifest = {
            "apiVersion": "karpenter.k8s.aws/v1",
            "kind": "EC2NodeClass",
            "metadata": {"name": "gvisor"},
            "spec": {
                "role": self.karpenter_node_role.role_name,
                "amiSelectorTerms": [
                    {
                        "alias": "al2023@latest",
                    }
                ],
                "subnetSelectorTerms": [
                    {
                        "tags": {
                            "karpenter.sh/discovery": self.eks_cluster.cluster_name,
                        }
                    }
                ],
                "securityGroupSelectorTerms": [
                    {
                        "id": self.eks_cluster.cluster_security_group_id,
                    }
                ],
                "userData": """#!/bin/bash
set -ex

# Install gVisor
ARCH=$(uname -m)
URL=https://storage.googleapis.com/gvisor/releases/release/latest/${ARCH}

# Download and install runsc binary
cd /tmp
wget ${URL}/runsc ${URL}/runsc.sha512 ${URL}/containerd-shim-runsc-v1 ${URL}/containerd-shim-runsc-v1.sha512
sha512sum -c runsc.sha512
sha512sum -c containerd-shim-runsc-v1.sha512
rm -f *.sha512
chmod a+rx runsc containerd-shim-runsc-v1
mv runsc containerd-shim-runsc-v1 /usr/local/bin/

# Wait for containerd config to exist (nodeadm creates it)
while [ ! -f /etc/containerd/config.toml ]; do
  echo "Waiting for /etc/containerd/config.toml to be created..."
  sleep 2
done

# Add gVisor runtime config directly to containerd config
cat <<'GVISOR_EOF' >> /etc/containerd/config.toml

[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runsc]
  runtime_type = "io.containerd.runsc.v1"
GVISOR_EOF

# Restart containerd to pick up gVisor runtime
systemctl restart containerd

echo "gVisor installation completed"
""",
                "blockDeviceMappings": [
                    {
                        "deviceName": "/dev/xvda",
                        "ebs": {
                            "volumeSize": "150Gi",
                            "volumeType": "gp3",
                            "deleteOnTermination": True,
                        },
                    }
                ],
            },
        }

        # NodePool manifest for gVisor nodes
        gvisor_node_pool_manifest = {
            "apiVersion": "karpenter.sh/v1",
            "kind": "NodePool",
            "metadata": {"name": "gvisor"},
            "spec": {
                "template": {
                    "metadata": {
                        "labels": {
                            "runtime": "gvisor",
                            "workload": "sandbox",
                        },
                    },
                    "spec": {
                        "requirements": [
                            {
                                "key": "kubernetes.io/arch",
                                "operator": "In",
                                "values": ["amd64"],
                            },
                            {
                                "key": "kubernetes.io/os",
                                "operator": "In",
                                "values": ["linux"],
                            },
                            {
                                "key": "karpenter.sh/capacity-type",
                                "operator": "In",
                                "values": ["on-demand"],
                            },
                            {
                                "key": "karpenter.k8s.aws/instance-category",
                                "operator": "In",
                                "values": ["c", "m", "t"],
                            },
                            {
                                "key": "karpenter.k8s.aws/instance-generation",
                                "operator": "Gt",
                                "values": ["2"],
                            },
                        ],
                        "nodeClassRef": {
                            "group": "karpenter.k8s.aws",
                            "kind": "EC2NodeClass",
                            "name": "gvisor",
                        },
                        "taints": [
                            {
                                "key": "runtime",
                                "value": "gvisor",
                                "effect": "NoSchedule",
                            }
                        ],
                        "expireAfter": "720h",
                    },
                },
                "disruption": {
                    "consolidationPolicy": "WhenEmptyOrUnderutilized",
                    "consolidateAfter": "5m",
                },
                "limits": {
                    "cpu": "500",
                    "memory": "500Gi",
                },
                "weight": 5,
            },
        }

        # RuntimeClass manifest for gVisor
        gvisor_runtime_class_manifest = {
            "apiVersion": "node.k8s.io/v1",
            "kind": "RuntimeClass",
            "metadata": {"name": "gvisor"},
            "handler": "runsc",
            "scheduling": {
                "nodeSelector": {
                    "runtime": "gvisor",
                },
                "tolerations": [
                    {
                        "key": "runtime",
                        "value": "gvisor",
                        "effect": "NoSchedule",
                    }
                ],
            },
        }

        # Apply the manifests with dependency on Helm chart
        gvisor_ec2_node_class = aws_eks.KubernetesManifest(
            self,
            "GVisorEC2NodeClass",
            cluster=self.eks_cluster.cluster,
            manifest=[gvisor_ec2_node_class_manifest],
            prune=True,
            overwrite=True,
        )
        gvisor_ec2_node_class.node.add_dependency(self.helm_chart)

        gvisor_node_pool = aws_eks.KubernetesManifest(
            self,
            "GVisorNodePool",
            cluster=self.eks_cluster.cluster,
            manifest=[gvisor_node_pool_manifest],
            prune=True,
            overwrite=True,
        )
        gvisor_node_pool.node.add_dependency(self.helm_chart)
        gvisor_node_pool.node.add_dependency(gvisor_ec2_node_class)

        gvisor_runtime_class = aws_eks.KubernetesManifest(
            self,
            "GVisorRuntimeClass",
            cluster=self.eks_cluster.cluster,
            manifest=[gvisor_runtime_class_manifest],
            prune=True,
            overwrite=True,
        )
        gvisor_runtime_class.node.add_dependency(self.helm_chart)

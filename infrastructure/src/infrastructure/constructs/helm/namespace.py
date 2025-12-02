# help

from constructs import Construct
from infrastructure.constructs.aws.eks_cluster import EksCluster


class KubernetesNamespace(Construct):
    """
    A construct to create a Kubernetes Namespace in an EKS cluster.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        name: str,
    ) -> None:
        super().__init__(scope, construct_id)

        # Define the namespace manifest
        manifest = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": {
                    "name": name,
                },
            },
        }

        # Create the namespace in the cluster
        self.resource = eks_cluster.cluster.add_manifest(
            construct_id,
            manifest,
        )

        # Expose the namespace name
        self.name = name

from constructs import Construct
from typing import Any
from infrastructure.constructs.aws.eks_cluster import EksCluster


class StorageClass(Construct):
    """Creates a Kubernetes StorageClass"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        name: str,
        provisioner: str,
        parameters: dict[str, Any] | None = None,
        reclaim_policy: str = "Delete",
        allow_volume_expansion: bool = True,
        volume_binding_mode: str = "WaitForFirstConsumer",
        is_default: bool = False,
    ) -> None:
        super().__init__(scope, construct_id)

        self.eks_cluster = eks_cluster
        self.name = name
        self.provisioner = provisioner
        self.parameters = parameters or {}
        self.reclaim_policy = reclaim_policy
        self.allow_volume_expansion = allow_volume_expansion
        self.volume_binding_mode = volume_binding_mode
        self.is_default = is_default

        # Create the StorageClass
        self.storage_class = self._create_storage_class()

    def _create_storage_class(self):
        """Create the Kubernetes StorageClass manifest"""

        storage_class_manifest = {
            "apiVersion": "storage.k8s.io/v1",
            "kind": "StorageClass",
            "metadata": {
                "name": self.name,
            },
            "provisioner": self.provisioner,
            "parameters": self.parameters,
            "reclaimPolicy": self.reclaim_policy,
            "allowVolumeExpansion": self.allow_volume_expansion,
            "volumeBindingMode": self.volume_binding_mode,
        }

        # Add default storage class annotation if specified
        if self.is_default:
            storage_class_manifest["metadata"]["annotations"] = {
                "storageclass.kubernetes.io/is-default-class": "true"
            }

        return self.eks_cluster.cluster.add_manifest(
            f"StorageClass-{self.name}",
            storage_class_manifest,
        )

    @classmethod
    def create_ebs_gp3_storage_class(
        cls,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        name: str = "ebs-gp3",
        is_default: bool = False,
        encrypted: bool = True,
        **kwargs,
    ) -> "StorageClass":
        """
        Create an EBS GP3 StorageClass with common defaults

        Args:
            scope: CDK scope
            construct_id: Construct ID
            eks_cluster: EKS cluster
            name: StorageClass name
            is_default: Whether this should be the default storage class
            encrypted: Whether volumes should be encrypted
            **kwargs: Additional parameters to override defaults
        """
        default_parameters = {
            "type": "gp3",
            "encrypted": "true" if encrypted else "false",
            "fsType": "ext4",
        }

        # Override with any provided parameters
        parameters = {**default_parameters, **kwargs.get("parameters", {})}

        return cls(
            scope=scope,
            construct_id=construct_id,
            eks_cluster=eks_cluster,
            name=name,
            provisioner="ebs.csi.eks.amazonaws.com",
            parameters=parameters,
            is_default=is_default,
            **{k: v for k, v in kwargs.items() if k != "parameters"},
        )

    @classmethod
    def create_ebs_gp2_storage_class(
        cls,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        name: str = "ebs-gp2",
        is_default: bool = False,
        encrypted: bool = True,
        **kwargs,
    ) -> "StorageClass":
        """
        Create an EBS GP2 StorageClass with common defaults

        Args:
            scope: CDK scope
            construct_id: Construct ID
            eks_cluster: EKS cluster
            name: StorageClass name
            is_default: Whether this should be the default storage class
            encrypted: Whether volumes should be encrypted
            **kwargs: Additional parameters to override defaults
        """
        default_parameters = {
            "type": "gp2",
            "encrypted": "true" if encrypted else "false",
            "fsType": "ext4",
        }

        # Override with any provided parameters
        parameters = {**default_parameters, **kwargs.get("parameters", {})}

        return cls(
            scope=scope,
            construct_id=construct_id,
            eks_cluster=eks_cluster,
            name=name,
            provisioner="ebs.csi.eks.amazonaws.com",
            parameters=parameters,
            is_default=is_default,
            **{k: v for k, v in kwargs.items() if k != "parameters"},
        )

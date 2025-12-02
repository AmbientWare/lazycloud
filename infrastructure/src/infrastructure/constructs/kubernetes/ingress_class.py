from constructs import Construct
from typing import Any
from infrastructure.constructs.aws.eks_cluster import EksCluster


class IngressClass(Construct):
    """Creates a Kubernetes IngressClass"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        name: str,
        controller: str,
        parameters: dict[str, Any] | None = None,
        is_default: bool = False,
    ) -> None:
        super().__init__(scope, construct_id)

        self.eks_cluster = eks_cluster
        self.name = name
        self.controller = controller
        self.parameters = parameters
        self.is_default = is_default

        # Create the IngressClass
        self.ingress_class = self._create_ingress_class()

    def _create_ingress_class(self):
        """Create the Kubernetes IngressClass manifest"""

        ingress_class_manifest = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "IngressClass",
            "metadata": {
                "name": self.name,
            },
            "spec": {
                "controller": self.controller,
            },
        }

        # Add default ingress class annotation if specified
        if self.is_default:
            ingress_class_manifest["metadata"]["annotations"] = {
                "ingressclass.kubernetes.io/is-default-class": "true"
            }

        # Add parameters if provided
        if self.parameters:
            ingress_class_manifest["spec"]["parameters"] = self.parameters

        return self.eks_cluster.cluster.add_manifest(
            f"IngressClass-{self.name}",
            ingress_class_manifest,
        )

    @classmethod
    def create_alb_ingress_class(
        cls,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        name: str = "alb",
        is_default: bool = True,
        scheme: str = "internet-facing",
        certificate_arn: str | None = None,
        group_name: str | None = None,
        **kwargs,
    ) -> "IngressClass":
        """
        Create an ALB IngressClass with IngressClassParams for AWS Load Balancer Controller

        Args:
            scope: CDK scope
            construct_id: Construct ID
            eks_cluster: EKS cluster
            name: IngressClass name
            is_default: Whether this should be the default ingress class
            scheme: ALB scheme (internet-facing or internal)
            certificate_arn: ARN of the SSL certificate to use
            group_name: Group name to share ALB across multiple Ingress resources
            **kwargs: Additional parameters
        """
        # First create the IngressClassParams
        ingress_class_params_spec: dict[str, Any] = {
            "scheme": scheme,
        }

        # Add group name to share ALB across multiple Ingress resources
        if group_name:
            ingress_class_params_spec["group"] = {"name": group_name}

        # Add certificate ARN if provided
        if certificate_arn:
            ingress_class_params_spec["certificateARNs"] = [certificate_arn]

        ingress_class_params_manifest = {
            "apiVersion": "eks.amazonaws.com/v1",
            "kind": "IngressClassParams",
            "metadata": {
                "name": name,
            },
            "spec": ingress_class_params_spec,
        }

        # Add the IngressClassParams to the cluster
        ingress_class_params = eks_cluster.cluster.add_manifest(
            f"IngressClassParams-{name}",
            ingress_class_params_manifest,
        )

        # Create parameters reference for the IngressClass
        parameters = {
            "apiGroup": "eks.amazonaws.com",
            "kind": "IngressClassParams",
            "name": name,
        }

        ingress_class = cls(
            scope=scope,
            construct_id=construct_id,
            eks_cluster=eks_cluster,
            name=name,
            controller="eks.amazonaws.com/alb",
            parameters=parameters,
            is_default=is_default,
            **kwargs,
        )

        # Store reference to params for dependency management
        ingress_class.ingress_class_params = ingress_class_params

        return ingress_class

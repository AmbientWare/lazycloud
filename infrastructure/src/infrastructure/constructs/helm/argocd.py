from typing import Any

import aws_cdk as cdk
from aws_cdk import aws_eks
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.aws.eks_cluster import EksCluster
from infrastructure.constructs.helm.namespace import KubernetesNamespace


class ArgoCDConstruct(Construct):
    """ArgoCD deployment on EKS using Helm"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        eks_cluster: EksCluster,
        config: EnvironmentConfig,
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster

        # Create ArgoCD namespace
        self._create_namespace()

        # Deploy ArgoCD via Helm
        self.helm_chart = self._deploy_argocd_helm()

        # Create custom ALB ingress if domain is provided
        self.ingress = self._create_alb_ingress()

    def _create_namespace(self) -> None:
        """Create ArgoCD namespace"""
        self.namespace = KubernetesNamespace(
            self,
            "ArgoCDNamespace",
            eks_cluster=self.eks_cluster,
            name="argocd",
        )

    def _deploy_argocd_helm(self) -> aws_eks.HelmChart:
        """Deploy ArgoCD using Helm chart"""
        return self.eks_cluster.cluster.add_helm_chart(
            "ArgoCD",
            chart="argo-cd",
            repository="https://argoproj.github.io/argo-helm",
            namespace=self.namespace.name,
            version="5.51.6",
            wait=True,
            timeout=cdk.Duration.minutes(10),
            values=self._get_helm_values(),
        )

    def _get_helm_values(self) -> dict[str, Any]:
        """Get Helm values for ArgoCD deployment"""
        values = {
            # Global configuration
            "global": {
                "domain": f"argocd.{self.config.domain_name}"
                if self.config.domain_name
                else None,
            },
            # Override default names to avoid CDK's long auto-generated names
            "nameOverride": "argocd",
            "fullnameOverride": "argocd",
            # Server configuration
            "server": {
                "service": {
                    "type": "ClusterIP",
                },
                "config": {
                    "users.anonymous.enabled": "false",
                    "url": f"https://argocd.{self.config.domain_name}"
                    if self.config.domain_name
                    else None,
                    "helm.enableOCI": "true",
                },
                "resources": {
                    "limits": {
                        "cpu": "1000m",
                        "memory": "512Mi",
                    },
                    "requests": {
                        "cpu": "250m",
                        "memory": "256Mi",
                    },
                },
                "metrics": {
                    "enabled": True,
                },
            },
            # Application Controller configuration
            "controller": {
                "resources": {
                    "limits": {
                        "cpu": "2000m",
                        "memory": "1Gi",
                    },
                    "requests": {
                        "cpu": "500m",
                        "memory": "512Mi",
                    },
                },
                "metrics": {
                    "enabled": True,
                },
            },
            # Dex configuration (OAuth2 proxy)
            "dex": {
                "enabled": True,
                "resources": {
                    "limits": {
                        "cpu": "50m",
                        "memory": "64Mi",
                    },
                    "requests": {
                        "cpu": "10m",
                        "memory": "32Mi",
                    },
                },
            },
            # Redis configuration
            "redis": {
                "resources": {
                    "limits": {
                        "cpu": "200m",
                        "memory": "128Mi",
                    },
                    "requests": {
                        "cpu": "100m",
                        "memory": "64Mi",
                    },
                },
            },
            # Repo Server configuration
            "repoServer": {
                "resources": {
                    "limits": {
                        "cpu": "2000m",
                        "memory": "1Gi",
                    },
                    "requests": {
                        "cpu": "250m",
                        "memory": "256Mi",
                    },
                },
                "metrics": {
                    "enabled": True,
                },
            },
            # ApplicationSet Controller
            "applicationSet": {
                "enabled": True,
                "resources": {
                    "limits": {
                        "cpu": "100m",
                        "memory": "128Mi",
                    },
                    "requests": {
                        "cpu": "100m",
                        "memory": "128Mi",
                    },
                },
            },
            # Notifications
            "notifications": {
                "enabled": False,
            },
        }

        # Add ingress configuration if domain is provided
        if self.config.domain_name:
            # Disable default ingress since we'll create our own custom ALB ingress
            values["server"]["ingress"] = {
                "enabled": False,
            }

        return values

    def _create_alb_ingress(self):
        """Create custom ALB ingress for ArgoCD with GRPC support"""
        ingress_manifest = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": "argocd-alb",
                "namespace": self.namespace.name,
                "annotations": {
                    "alb.ingress.kubernetes.io/conditions.argogrpc": '[{"field":"http-header","httpHeaderConfig":{"httpHeaderName": "Content-Type", "values":["application/grpc"]}}]',
                    "alb.ingress.kubernetes.io/backend-protocol": "HTTPS",
                    "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
                    "alb.ingress.kubernetes.io/scheme": "internet-facing",
                    "external-dns.alpha.kubernetes.io/hostname": f"argocd.{self.config.domain_name}",
                },
                "labels": {"app": "alb-ingress-controller"},
            },
            "spec": {
                "ingressClassName": "alb",
                "rules": [
                    {
                        "host": f"argocd.{self.config.domain_name}",
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": "argogrpc",
                                            "port": {"number": 443},
                                        }
                                    },
                                },
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": "argocd-server",
                                            "port": {"number": 443},
                                        }
                                    },
                                },
                            ]
                        },
                    }
                ],
            },
        }

        # Add the ingress manifest to the cluster
        ingress = self.eks_cluster.cluster.add_manifest(
            "ArgoCDALBIngress", ingress_manifest
        )

        # Ensure ingress is created after namespace and helm chart
        ingress.node.add_dependency(self.namespace)
        ingress.node.add_dependency(self.helm_chart)

        return ingress

    @property
    def argocd_namespace(self) -> str:
        """Get ArgoCD namespace"""
        return self.namespace.name

    @property
    def server_service_name(self) -> str:
        """Get ArgoCD server service name"""
        return "argocd-server"

    @property
    def port_forward_command(self) -> str:
        """Get kubectl port-forward command"""
        return (
            f"kubectl port-forward -n {self.namespace.name} svc/argocd-server 8080:80"
        )

    @property
    def setup_instructions(self) -> str:
        """Get setup instructions"""
        return f"""
# ArgoCD Setup Instructions:

1. Port-forward to access ArgoCD UI:
   {self.port_forward_command}

2. Access ArgoCD UI at: http://localhost:8080

3. Get initial admin password:
   kubectl -n {self.namespace.name} get secret argocd-initial-admin-secret -o jsonpath="{{.data.password}}" | base64 -d

4. Login with username 'admin' and the password from step 3

{f"5. Or access via LoadBalancer (check service): kubectl get svc -n {self.namespace.name} argocd-server" if not self.config.domain_name else f"5. Or access via ingress at: https://argocd.{self.config.domain_name}"}
"""

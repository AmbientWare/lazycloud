from typing import Any
import yaml

import aws_cdk as cdk
from constructs import Construct

from infrastructure.constructs.aws.eks_cluster import EksCluster
from infrastructure.config.environments import EnvironmentConfig


class ArgoCDApplication(Construct):
    """Helper construct to create an ArgoCD Application CR easily.

    Example usage:
    >>> ArgoCDApplication(
    ...     self,
    ...     "MyApp",
    ...     eks_cluster=eks_cluster,
    ...     name="my-app",
    ...     repo_url="https://github.com/org/repo.git",
    ...     repo_path="charts/my-app",
    ...     dest_namespace="default",
    ... )
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        eks_cluster: EksCluster,
        name: str,
        repo_url: str,
        repo_path: str | None = None,
        chart: str | None = None,
        target_revision: str = "HEAD",
        dest_namespace: str = "default",
        dest_server: str = "https://kubernetes.default.svc",
        project: str = "default",
        automated_sync: bool = True,
        prune: bool = True,
        self_heal: bool = True,
        config: EnvironmentConfig | None = None,
        helm_values: dict[str, Any] | None = None,
        create_namespace: bool = True,
        server_side_apply: bool = True,
    ) -> None:
        super().__init__(scope, construct_id)

        if repo_path is None and chart is None:
            repo_path = "."
        elif repo_path is not None and chart is not None:
            raise ValueError("Only one of repo_path or chart can be provided")

        self.eks_cluster = eks_cluster
        self.name = name
        self.project = project
        self.config = config
        self.helm_values = helm_values
        self.repo_path = repo_path
        self.chart = chart

        # Build Application manifest
        self.manifest: dict[str, Any] = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "Application",
            "metadata": {
                "name": self.name,
                "namespace": "argocd",
                "labels": {
                    "app.kubernetes.io/managed-by": "cdk",
                },
                "finalizers": ["resources-finalizer.argocd.argoproj.io"],
            },
            "spec": {
                "project": self.project,
                "source": self._build_source_spec(
                    repo_url, repo_path, chart, target_revision, helm_values
                ),
                "destination": {
                    "server": dest_server,
                    "namespace": dest_namespace,
                },
            },
        }

        # Always add syncOptions
        self.manifest["spec"]["syncPolicy"] = {
            "syncOptions": [
                f"CreateNamespace={'true' if create_namespace else 'false'}",
                f"ServerSideApply={'true' if server_side_apply else 'false'}",
            ]
        }

        if automated_sync:
            self.manifest["spec"]["syncPolicy"]["automated"] = {
                "prune": prune,
                "selfHeal": self_heal,
            }

        # Apply manifest to the cluster
        self.eks_cluster.cluster.add_manifest(
            f"{construct_id}ArgoCDApplication",
            self.manifest,
        )

        # Output optional convenience info
        cdk.CfnOutput(
            self,
            f"{construct_id}ArgoAppName",
            value=self.name,
            description="ArgoCD Application name",
        )

    def _build_source_spec(
        self,
        repo_url: str,
        repo_path: str | None,
        chart: str | None,
        target_revision: str,
        helm_values: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the source specification for the ArgoCD Application"""
        source_spec: dict[str, str | dict[str, str]] = {
            "repoURL": repo_url,
            "targetRevision": target_revision,
        }

        if chart is not None:
            source_spec["chart"] = chart
        elif repo_path is not None:
            source_spec["path"] = repo_path

        if helm_values:
            source_spec["helm"] = {
                "values": yaml.dump(helm_values, default_flow_style=False)
            }

        return source_spec

    @property
    def application_name(self) -> str:
        """Return the ArgoCD Application name"""
        return self.name

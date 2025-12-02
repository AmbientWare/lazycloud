from typing import Any
import aws_cdk as cdk
from constructs import Construct

from infrastructure.config.environments import EnvironmentConfig
from infrastructure.constructs.apps.base import ArgoCDApplication
from infrastructure.constructs.aws.eks_cluster import EksCluster


class AppConfig:
    """Configuration for a single application"""

    def __init__(
        self,
        name: str,
        repo_url: str,
        target_revision: str,
        dest_namespace: str,
        repo_path: str | None = None,
        chart: str | None = None,
        automated_sync: bool = True,
        prune: bool = True,
        self_heal: bool = True,
        project: str = "default",
        helm_values: dict[str, Any] = {},
    ) -> None:
        self.name = name
        self.repo_url = repo_url
        self.repo_path = repo_path
        self.chart = chart
        self.target_revision = target_revision
        self.dest_namespace = dest_namespace
        self.automated_sync = automated_sync
        self.prune = prune
        self.self_heal = self_heal
        self.project = project
        self.helm_values = helm_values or {}


class EnvironmentApps(Construct):
    """Construct to deploy environment-specific applications to ArgoCD"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: EnvironmentConfig,
        eks_cluster: EksCluster,
        apps: list[AppConfig],
    ) -> None:
        super().__init__(scope, construct_id)

        self.config = config
        self.eks_cluster = eks_cluster
        self.apps_config = apps

        # Store created applications
        self.applications: dict[str, ArgoCDApplication] = {}

        # Create ArgoCD applications for all configured apps
        self._create_applications()

        # Create outputs
        self._create_outputs()

    def _create_applications(self) -> None:
        """Create ArgoCD applications for all configured apps"""
        for app_config in self.apps_config:
            # Create construct ID by converting app name to PascalCase
            construct_id = self._to_pascal_case(app_config.name) + "App"

            # Create ArgoCD application name
            app_name = f"{app_config.name}-{self.config.environment}"

            app = ArgoCDApplication(
                self,
                construct_id,
                eks_cluster=self.eks_cluster,
                name=app_name,
                repo_url=app_config.repo_url,
                repo_path=app_config.repo_path,
                chart=app_config.chart,
                target_revision=app_config.target_revision,
                dest_namespace=app_config.dest_namespace,
                project=app_config.project,
                automated_sync=app_config.automated_sync,
                prune=app_config.prune,
                self_heal=app_config.self_heal,
                config=self.config,
                helm_values=app_config.helm_values,
            )

            # Store the application for easy access
            self.applications[app_config.name] = app

    def _to_pascal_case(self, snake_str: str) -> str:
        """Convert snake_case or kebab-case string to PascalCase"""
        return "".join(
            word.capitalize() for word in snake_str.replace("-", "_").split("_")
        )

    def _create_outputs(self) -> None:
        """Create CloudFormation outputs for the applications"""
        if not self.applications:
            return

        # Output the list of deployed applications
        cdk.CfnOutput(
            self,
            "DeployedApplications",
            value=", ".join(
                [app.application_name for app in self.applications.values()]
            ),
            description=f"ArgoCD applications deployed for {self.config.environment} environment",
        )

        # Output the number of applications deployed
        cdk.CfnOutput(
            self,
            "ApplicationCount",
            value=str(len(self.applications)),
            description="Number of ArgoCD applications deployed",
        )

    def get_all_applications(self) -> list[ArgoCDApplication]:
        """Get list of all deployed applications"""
        return list(self.applications.values())

    @property
    def application_names(self) -> list[str]:
        """Get list of all deployed application names"""
        return [app.application_name for app in self.applications.values()]

    def get_application(self, service_name: str) -> ArgoCDApplication:
        """Get a specific application by service name"""
        if service_name not in self.applications:
            raise ValueError(f"Application for service '{service_name}' not found")

        return self.applications[service_name]

    def has_application(self, service_name: str) -> bool:
        """Check if an application exists for the given service name"""
        return service_name in self.applications

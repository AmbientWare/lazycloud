"""Flow deployment registry for Prefect flows.

This module provides a centralized registry for all flow deployments,
ensuring consistent naming and easy triggering from the API.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prefect import Flow


class WorkPool(str, Enum):
    """Available work pools for flow execution."""

    BACKGROUND = "lazycloud-background"


@dataclass(frozen=True)
class FlowDeployment:
    """Registry entry for a flow deployment.

    This class holds the flow reference and deployment configuration,
    providing consistent naming for both deployment creation and triggering.
    """

    flow: "Flow"
    deployment_name: str
    work_pool: WorkPool = WorkPool.BACKGROUND

    @property
    def flow_name(self) -> str:
        """Get the flow name from the flow function."""
        return self.flow.name

    @property
    def full_name(self) -> str:
        """Get the full deployment name for run_deployment().

        Format: 'flow-name/deployment-name'
        """
        return f"{self.flow_name}/{self.deployment_name}"

    def to_deployment(self, **kwargs):
        """Create a Prefect deployment object for serving."""
        return self.flow.to_deployment(
            name=self.deployment_name,
            work_pool_name=self.work_pool.value,
            **kwargs,
        )


class _DeploymentsMeta(type):
    """Metaclass for lazy initialization of flow deployments.

    This pattern avoids the deprecated @classmethod + @property combination
    while still providing lazy initialization to prevent circular imports.
    """

    _initialized: bool = False
    _deployments: dict[str, FlowDeployment] = {}

    def _ensure_initialized(cls) -> None:
        """Initialize deployments on first access."""
        if cls._initialized:
            return

        # Import flows here to avoid circular imports
        from backend.prefect_app.flows.deploy import deploy_compose_flow
        from backend.prefect_app.flows.destroy import destroy_compose_flow
        from backend.prefect_app.flows.instances import delete_instance_flow
        from backend.prefect_app.flows.rollback import rollback_compose_flow
        from backend.prefect_app.flows.services import (
            restart_all_services_flow,
            restart_service_flow,
        )

        cls._deployments = {
            "DEPLOY_COMPOSE": FlowDeployment(
                flow=deploy_compose_flow,
                deployment_name="deploy-compose",
            ),
            "DESTROY_COMPOSE": FlowDeployment(
                flow=destroy_compose_flow,
                deployment_name="destroy-compose",
            ),
            "ROLLBACK_COMPOSE": FlowDeployment(
                flow=rollback_compose_flow,
                deployment_name="rollback-compose",
            ),
            "DELETE_INSTANCE": FlowDeployment(
                flow=delete_instance_flow,
                deployment_name="delete-instance",
            ),
            "RESTART_SERVICE": FlowDeployment(
                flow=restart_service_flow,
                deployment_name="restart-service",
            ),
            "RESTART_ALL_SERVICES": FlowDeployment(
                flow=restart_all_services_flow,
                deployment_name="restart-all-services",
            ),
        }
        cls._initialized = True

    def __getattr__(cls, name: str) -> FlowDeployment:
        """Get deployment by attribute name (e.g., Deployments.DEPLOY_COMPOSE)."""
        cls._ensure_initialized()
        if name in cls._deployments:
            return cls._deployments[name]
        raise AttributeError(f"Deployment '{name}' not found in registry")


class Deployments(metaclass=_DeploymentsMeta):
    """Central registry of all flow deployments.

    Usage:
        # Triggering a flow
        from backend.prefect_app.client import run_flow
        from backend.prefect_app.registry import Deployments

        flow_run_id = await run_flow(
            Deployments.DEPLOY_COMPOSE,
            {"deployment_id": "..."}
        )

        # Getting deployment name
        name = Deployments.DEPLOY_COMPOSE.full_name
        # -> "deploy-compose-flow/deploy-compose"
    """

    # Type hints for IDE support
    DEPLOY_COMPOSE: FlowDeployment
    DESTROY_COMPOSE: FlowDeployment
    ROLLBACK_COMPOSE: FlowDeployment
    DELETE_INSTANCE: FlowDeployment
    RESTART_SERVICE: FlowDeployment
    RESTART_ALL_SERVICES: FlowDeployment

    @classmethod
    def all(cls) -> list[FlowDeployment]:
        """Get all registered deployments."""
        cls._ensure_initialized()
        return list(cls._deployments.values())

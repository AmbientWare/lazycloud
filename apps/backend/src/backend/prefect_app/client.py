"""Prefect client utilities for triggering flow runs.

This module provides a clean interface for triggering flow runs from the API,
using the flow deployment registry for consistent naming.
"""

from uuid import UUID

from loguru import logger
from prefect.client.orchestration import get_client

from backend.prefect_app.registry import FlowDeployment

# Cache deployment IDs to avoid repeated lookups
_deployment_id_cache: dict[str, UUID] = {}


async def run_flow(
    deployment: FlowDeployment,
    parameters: dict,
) -> str:
    """Trigger a flow run and return the flow_run_id.

    Args:
        deployment: The FlowDeployment from the registry
        parameters: Parameters to pass to the flow

    Returns:
        The flow run ID as a string

    Example:
        from backend.prefect_app.registry import Deployments
        from backend.prefect_app.client import run_flow

        flow_run_id = await run_flow(
            Deployments.DEPLOY_COMPOSE,
            {"deployment_id": "..."}
        )
    """
    async with get_client() as client:
        # Cache deployment IDs to avoid repeated lookups
        if deployment.full_name not in _deployment_id_cache:
            try:
                prefect_deployment = await client.read_deployment_by_name(
                    deployment.full_name
                )
                _deployment_id_cache[deployment.full_name] = prefect_deployment.id
                logger.debug(
                    f"Cached deployment ID for {deployment.full_name}: {prefect_deployment.id}"
                )
            except Exception as e:
                logger.error(f"Failed to read deployment {deployment.full_name}: {e}")
                raise ValueError(
                    f"Deployment {deployment.full_name} not found. "
                    "Ensure the worker is running and deployments are registered."
                ) from e

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=_deployment_id_cache[deployment.full_name],
            parameters=parameters,
        )

        logger.info(
            f"Created flow run {flow_run.id} for {deployment.full_name} "
            f"with parameters: {list(parameters.keys())}"
        )

        return str(flow_run.id)


async def get_flow_run_status(flow_run_id: str) -> dict:
    """Get the status of a flow run.

    Args:
        flow_run_id: The flow run ID

    Returns:
        A dict with 'state_type', 'state_name', and 'message'
    """
    async with get_client() as client:
        flow_run = await client.read_flow_run(UUID(flow_run_id))

        return {
            "flow_run_id": str(flow_run.id),
            "state_type": flow_run.state.type.value if flow_run.state else None,
            "state_name": flow_run.state.name if flow_run.state else None,
            "message": flow_run.state.message if flow_run.state else None,
        }


def clear_deployment_cache() -> None:
    """Clear the deployment ID cache.

    Useful for testing or after redeploying flows.
    """
    _deployment_id_cache.clear()
    logger.debug("Cleared deployment ID cache")

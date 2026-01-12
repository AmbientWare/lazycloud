"""Prefect flows for background operations.

These flows are executed by Prefect workers polling from work pools,
providing more reliable execution than WebSocket-based task workers.
"""

from backend.prefect_app.flows.deploy import deploy_compose_flow
from backend.prefect_app.flows.destroy import destroy_compose_flow
from backend.prefect_app.flows.instances import delete_instance_flow
from backend.prefect_app.flows.rollback import rollback_compose_flow
from backend.prefect_app.flows.services import (
    restart_all_services_flow,
    restart_service_flow,
)

__all__ = [
    "deploy_compose_flow",
    "destroy_compose_flow",
    "rollback_compose_flow",
    "delete_instance_flow",
    "restart_service_flow",
    "restart_all_services_flow",
]

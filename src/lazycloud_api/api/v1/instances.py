from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from lazycloud_api.api.security import UserData, get_current_active_user
from lazycloud_api.database import db
from lazycloud_api.prefect_app.instances import delete_instance_task
from shared.models.deployments import DeploymentStates
from shared.models.statuses import TaskStatus
from shared.responses.tasks import InstanceTaskStatusResponse

instances_router = APIRouter(prefix="/instances", tags=["instances"])


@instances_router.delete(
    "/{deployment_id}/{service_name}/{pod_name}",
)
async def delete_instance(
    deployment_id: str,
    service_name: str,
    pod_name: str,
    current_user: UserData = Depends(get_current_active_user),
) -> InstanceTaskStatusResponse:
    """Delete a specific instance in a deployment."""
    try:
        # Verify deployment exists and belongs to user
        deployment = await db.compose_deployments.afind_one(
            {
                "id": deployment_id,
                "user_id": current_user.user_id,
            }
        )

        if not deployment:
            raise HTTPException(status_code=404, detail="Deployment not found")

        # Check deployment state - don't allow instance deletion if deployment is being deleted
        if deployment.state in [DeploymentStates.DELETING]:
            raise HTTPException(
                status_code=400,
                detail="Cannot delete instance: deployment is being deleted",
            )

        # Basic validation that service exists
        helm_values = deployment.helm_values
        if not helm_values or not helm_values.services:
            raise HTTPException(
                status_code=400,
                detail="Deployment does not have service configuration",
            )

        service = next(
            (s for s in helm_values.services if s.name == service_name), None
        )
        if not service:
            raise HTTPException(
                status_code=404,
                detail=f"Service '{service_name}' not found in deployment",
            )

        # Submit task to delete the instance
        logger.info(
            f"Submitting delete instance task for pod {pod_name} in deployment {deployment_id}"
        )

        task_future = delete_instance_task.delay(
            deployment_id=deployment_id,
            service_name=service_name,
            pod_name=pod_name,
            user_id=current_user.user_id,
        )

        return InstanceTaskStatusResponse(
            task_id=task_future.task_run_id,
            status=TaskStatus.PENDING,
            message=f"Instance {pod_name} deletion task submitted",
            deployment_id=deployment_id,
            service_name=service_name,
            pod_name=pod_name,
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Failed to submit delete instance task: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to submit instance deletion task"
        )

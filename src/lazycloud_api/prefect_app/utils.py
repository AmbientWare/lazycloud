from typing import Any, cast
from uuid import UUID

from prefect.client.orchestration import get_client
from prefect.client.schemas.objects import FlowRun, TaskRun
from prefect.logging import get_logger

from shared.models.statuses import TaskStatus

logger = get_logger(__name__)


def _any_task_run_result(task_run: TaskRun) -> Any:
    try:
        result = cast(Any, task_run.state.result(_sync=True))  # type: ignore
        return result
    except Exception as e:
        logger.debug(f"Could not retrieve result for task run {task_run.id}: {e}")
        # Try to get exception from state
        if hasattr(task_run.state, "data") and task_run.state.data:
            if isinstance(task_run.state.data, dict):
                if "exception" in task_run.state.data:
                    return str(task_run.state.data["exception"])

        # Try to get message from state
        if hasattr(task_run.state, "message") and task_run.state.message:
            return task_run.state.message
        return None


def _any_flow_run_result(flow_run: FlowRun) -> Any:
    try:
        return cast(Any, flow_run.state.result(_sync=True))  # type: ignore

    except Exception as e:
        logger.warning(f"Could not retrieve result for flow run {flow_run.id}: {e}")
        return None


async def get_task_result(task_run_id: UUID) -> tuple[TaskStatus, Any]:
    """Get task or flow result or status. Handles both task runs and flow runs."""
    try:
        async with get_client() as client:
            # Try task run first
            try:
                task_run = await client.read_task_run(task_run_id)
                if not task_run.state:
                    return TaskStatus.PENDING, None

                if task_run.state.is_completed():
                    try:
                        _ = _any_task_run_result(task_run)
                        return TaskStatus.COMPLETED, "Task completed"
                    except Exception as e:
                        logger.warning(
                            f"Could not retrieve result for completed task run {task_run_id}: {e}"
                        )
                        return (
                            TaskStatus.COMPLETED,
                            "Task completed but could not retrieve result",
                        )

                elif task_run.state.is_failed():
                    try:
                        error_result = _any_task_run_result(task_run)
                        if error_result:
                            error_message = str(error_result)
                            # Clean up technical error messages for users
                            if (
                                "ImportError" in error_message
                                or "cannot import name" in error_message
                            ):
                                error_message = "Deployment failed due to a configuration error. Please contact support."
                            elif "exceeded quota" in error_message.lower() or (
                                "quota" in error_message.lower()
                                and "forbidden" in error_message.lower()
                            ):
                                error_message = "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."
                            elif (
                                "deployments.apps" in error_message
                                or "count/deployments" in error_message
                            ):
                                error_message = "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."
                        else:
                            # Fallback: try to get error from state attributes
                            if (
                                hasattr(task_run.state, "message")
                                and task_run.state.message
                            ):
                                error_message = task_run.state.message
                                # Clean up quota errors from state message too
                                if "exceeded quota" in error_message.lower() or (
                                    "quota" in error_message.lower()
                                    and "forbidden" in error_message.lower()
                                ):
                                    error_message = "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."
                            elif hasattr(task_run, "name"):
                                error_message = f"Task '{task_run.name}' failed"
                            else:
                                error_message = (
                                    "Task failed without specific error message."
                                )
                        return TaskStatus.ERROR, error_message
                    except Exception as e:
                        logger.warning(
                            f"Could not retrieve error result for failed task run {task_run_id}: {e}"
                        )
                        return (
                            TaskStatus.ERROR,
                            f"Task failed but could not retrieve error message: {str(e)}",
                        )

                else:
                    return TaskStatus.PENDING, None

            except Exception:
                # If not a task run, try flow run
                flow_run = await client.read_flow_run(task_run_id)
                if not flow_run.state:
                    return TaskStatus.PENDING, None

                if flow_run.state.is_completed():
                    try:
                        _ = _any_flow_run_result(flow_run)
                        return TaskStatus.COMPLETED, "Flow completed"
                    except Exception as e:
                        logger.warning(
                            f"Could not retrieve result for completed flow run {task_run_id}: {e}"
                        )
                        return (
                            TaskStatus.COMPLETED,
                            "Flow completed but could not retrieve result",
                        )

                elif flow_run.state.is_failed():
                    try:
                        error_result = _any_flow_run_result(flow_run)
                        if error_result:
                            error_message = str(error_result)
                            # Clean up technical error messages for users
                            if (
                                "ImportError" in error_message
                                or "cannot import name" in error_message
                            ):
                                error_message = "Deployment failed due to a configuration error. Please contact support."
                            elif "exceeded quota" in error_message.lower() or (
                                "quota" in error_message.lower()
                                and "forbidden" in error_message.lower()
                            ):
                                error_message = "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."
                            elif (
                                "deployments.apps" in error_message
                                or "count/deployments" in error_message
                            ):
                                error_message = "Resource limit exceeded. Please reduce the number of services, volumes, or upgrade your plan."
                        else:
                            error_message = (
                                "Flow failed without specific error message."
                            )
                        return TaskStatus.ERROR, error_message
                    except Exception as e:
                        logger.warning(
                            f"Could not retrieve error result for failed flow run {task_run_id}: {e}"
                        )
                        return (
                            TaskStatus.ERROR,
                            "Flow failed but could not retrieve error message",
                        )

                else:
                    return TaskStatus.PENDING, None

    except Exception as e:
        logger.error(f"Error checking task/flow status for {task_run_id}: {e}")
        return TaskStatus.ERROR, "Failed to check task/flow status"

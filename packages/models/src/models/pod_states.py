"""Pod state classification with Docker-like semantics.

This module provides a single source of truth for classifying Kubernetes pod states
into Docker-like states that users expect. It abstracts away Kubernetes complexity
and provides consistent state classification across all monitors.
"""

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel


class ContainerState(StrEnum):
    """Docker-like container states for user display.

    These map to familiar Docker states:
    - pending: Waiting to be scheduled
    - starting: Container creating, pulling image, initializing, health checks
    - running: Healthy and serving
    - updating: Rolling update in progress
    - restarting: Crash loop (CrashLoopBackOff)
    - stopping: Terminating gracefully
    - exited: Completed or stopped (replicas=0)
    - error: Unrecoverable failure
    """

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    UPDATING = "updating"
    RESTARTING = "restarting"
    STOPPING = "stopping"
    EXITED = "exited"
    ERROR = "error"


class PodFailureReasons:
    """All Kubernetes reasons that indicate pod failures.

    Unified constants from across the codebase for consistent error detection.
    """

    # Image-related failures (unrecoverable without config change)
    IMAGE_ERRORS = frozenset(
        {
            "ImagePullBackOff",
            "ErrImagePull",
            "ErrImageNeverPull",
            "InvalidImageName",
        }
    )

    # Container startup failures
    CONTAINER_ERRORS = frozenset(
        {
            "CreateContainerConfigError",
            "RunContainerError",
            "ContainerCannotRun",
        }
    )

    # Resource exhaustion
    RESOURCE_ERRORS = frozenset(
        {
            "OOMKilled",
            "Evicted",
        }
    )

    # Process exited (not a long-running service)
    EXIT_ERRORS = frozenset(
        {
            "ContainerExited",
            "Completed",
            "Error",
        }
    )

    # Restart loop
    RESTART_ERRORS = frozenset(
        {
            "CrashLoopBackOff",
        }
    )

    # Timeout/deadline
    TIMEOUT_ERRORS = frozenset(
        {
            "DeadlineExceeded",
        }
    )

    # All unrecoverable errors (need user intervention)
    UNRECOVERABLE = (
        IMAGE_ERRORS | CONTAINER_ERRORS | RESOURCE_ERRORS | TIMEOUT_ERRORS
    )

    # All errors that indicate failure
    ALL_FAILURES = UNRECOVERABLE | RESTART_ERRORS | EXIT_ERRORS


# User-friendly error messages for each failure reason
ERROR_MESSAGES: dict[str, str] = {
    # Image errors
    "ImagePullBackOff": "Failed to pull image",
    "ErrImagePull": "Failed to pull image",
    "ErrImageNeverPull": "Image not found locally and pull policy is Never",
    "InvalidImageName": "Invalid image name",
    # Container errors
    "CreateContainerConfigError": "Failed to create container configuration",
    "RunContainerError": "Failed to run container",
    "ContainerCannotRun": "Container cannot run",
    # Resource errors
    "OOMKilled": "Out of memory - increase memory limit",
    "Evicted": "Evicted due to resource pressure",
    # Exit errors
    "ContainerExited": "Service exited - use restart: no for one-time tasks",
    "Completed": "Service completed",
    "Error": "Container error",
    # Restart errors
    "CrashLoopBackOff": "Service keeps crashing",
    # Timeout errors
    "DeadlineExceeded": "Deadline exceeded",
}


@dataclass(frozen=True)
class PodInfo:
    """Minimal pod info needed for classification.

    Lightweight DTO to decouple from Kubernetes models.
    """

    name: str
    phase: str
    reason: str | None
    message: str | None
    ready_containers: int
    total_containers: int
    restart_count: int
    is_terminating: bool


@dataclass(frozen=True)
class PodStateResult:
    """Result of classifying a single pod's state."""

    state: ContainerState
    reason: str | None
    message: str
    is_error: bool
    is_recoverable: bool  # True for CrashLoopBackOff, False for image errors


class ContainerCounts(BaseModel):
    """Container counts for a service (Docker-like semantics)."""

    desired: int = 0
    running: int = 0
    starting: int = 0  # Creating, pulling, initializing, health checks
    pending: int = 0
    stopping: int = 0
    error: int = 0


@dataclass(frozen=True)
class ServiceStateResult:
    """Result of classifying a service's overall state."""

    state: ContainerState
    message: str
    is_ready: bool
    container_counts: ContainerCounts
    error_pod: str | None  # Name of first pod in error state


class PodStateClassifier:
    """Classifies Kubernetes pod states into Docker-like states.

    Single source of truth for all pod state classification in the codebase.

    Usage:
        classifier = PodStateClassifier()

        # Classify a single pod
        result = classifier.classify_pod(pod_info)

        # Classify an entire service
        result = classifier.classify_service(
            pods=[...],
            desired_replicas=3,
            updated_replicas=2
        )
    """

    def classify_pod(self, pod: PodInfo) -> PodStateResult:
        """Classify a single pod into a Docker-like state."""
        reason = pod.reason
        message = pod.message or ""

        # Check for terminating first
        if pod.is_terminating:
            return PodStateResult(
                state=ContainerState.STOPPING,
                reason=reason,
                message="Shutting down",
                is_error=False,
                is_recoverable=True,
            )

        # Check for crash loop (recoverable error)
        if reason in PodFailureReasons.RESTART_ERRORS:
            return PodStateResult(
                state=ContainerState.RESTARTING,
                reason=reason,
                message=self.get_user_message(reason, message),
                is_error=True,
                is_recoverable=True,
            )

        # Check for unrecoverable errors
        if reason in PodFailureReasons.UNRECOVERABLE:
            return PodStateResult(
                state=ContainerState.ERROR,
                reason=reason,
                message=self.get_user_message(reason, message),
                is_error=True,
                is_recoverable=False,
            )

        # Check for exit errors (process not long-running)
        if reason in PodFailureReasons.EXIT_ERRORS:
            return PodStateResult(
                state=ContainerState.ERROR,
                reason=reason,
                message=self.get_user_message(reason, message),
                is_error=True,
                is_recoverable=False,
            )

        # Map by Kubernetes phase
        phase = pod.phase

        if phase == "Succeeded":
            return PodStateResult(
                state=ContainerState.EXITED,
                reason=reason,
                message="Completed",
                is_error=False,
                is_recoverable=True,
            )

        if phase == "Failed":
            return PodStateResult(
                state=ContainerState.ERROR,
                reason=reason,
                message=message or "Failed",
                is_error=True,
                is_recoverable=False,
            )

        if phase == "Unknown":
            return PodStateResult(
                state=ContainerState.ERROR,
                reason=reason,
                message="Unknown state",
                is_error=True,
                is_recoverable=False,
            )

        if phase == "Pending":
            # Check for specific pending reasons
            if reason == "ContainerCreating":
                return PodStateResult(
                    state=ContainerState.STARTING,
                    reason=reason,
                    message="Pulling image",
                    is_error=False,
                    is_recoverable=True,
                )
            if reason == "PodInitializing":
                return PodStateResult(
                    state=ContainerState.STARTING,
                    reason=reason,
                    message="Initializing",
                    is_error=False,
                    is_recoverable=True,
                )
            return PodStateResult(
                state=ContainerState.PENDING,
                reason=reason,
                message="Waiting to start",
                is_error=False,
                is_recoverable=True,
            )

        if phase == "Running":
            # Check if all containers are ready (health checks passed)
            if (
                pod.ready_containers >= pod.total_containers
                and pod.total_containers > 0
            ):
                return PodStateResult(
                    state=ContainerState.RUNNING,
                    reason=reason,
                    message="Healthy",
                    is_error=False,
                    is_recoverable=True,
                )
            # Running but not ready - health checks pending
            return PodStateResult(
                state=ContainerState.STARTING,
                reason=reason,
                message="Running health checks",
                is_error=False,
                is_recoverable=True,
            )

        # Default to pending for unknown phases
        return PodStateResult(
            state=ContainerState.PENDING,
            reason=reason,
            message="Waiting",
            is_error=False,
            is_recoverable=True,
        )

    def classify_service(
        self,
        pods: list[PodInfo],
        desired_replicas: int,
        updated_replicas: int | None = None,
    ) -> ServiceStateResult:
        """Classify an entire service based on its pods."""
        # Handle stopped service
        if desired_replicas == 0:
            return ServiceStateResult(
                state=ContainerState.EXITED,
                message="Stopped",
                is_ready=True,
                container_counts=ContainerCounts(desired=0),
                error_pod=None,
            )

        # Handle no pods yet
        if not pods:
            return ServiceStateResult(
                state=ContainerState.PENDING,
                message="Waiting to start",
                is_ready=False,
                container_counts=ContainerCounts(desired=desired_replicas),
                error_pod=None,
            )

        # Classify each pod and count by state
        running = 0
        starting = 0
        pending = 0
        stopping = 0
        error = 0
        error_pod: str | None = None
        error_message: str | None = None
        has_restarting = False

        for pod in pods:
            result = self.classify_pod(pod)

            if result.state == ContainerState.RUNNING:
                running += 1
            elif result.state == ContainerState.STARTING:
                starting += 1
            elif result.state == ContainerState.PENDING:
                pending += 1
            elif result.state == ContainerState.STOPPING:
                stopping += 1
            elif result.state == ContainerState.RESTARTING:
                error += 1
                has_restarting = True
                if error_pod is None:
                    error_pod = pod.name
                    error_message = result.message
            elif result.state in (ContainerState.ERROR, ContainerState.EXITED):
                error += 1
                if error_pod is None:
                    error_pod = pod.name
                    error_message = result.message

        counts = ContainerCounts(
            desired=desired_replicas,
            running=running,
            starting=starting,
            pending=pending,
            stopping=stopping,
            error=error,
        )

        # Determine service state based on pod states

        # Error takes precedence (but restarting is a special error)
        if error > 0:
            if has_restarting:
                return ServiceStateResult(
                    state=ContainerState.RESTARTING,
                    message=error_message or "Service keeps crashing",
                    is_ready=False,
                    container_counts=counts,
                    error_pod=error_pod,
                )
            return ServiceStateResult(
                state=ContainerState.ERROR,
                message=error_message or "Error",
                is_ready=False,
                container_counts=counts,
                error_pod=error_pod,
            )

        # Check for rolling update
        if updated_replicas is not None and updated_replicas < desired_replicas:
            # Determine appropriate message based on what's happening
            if stopping > 0:
                message = "Updating service"
            elif starting > 0:
                message = "Rolling update"
            else:
                message = f"Updating ({updated_replicas}/{desired_replicas})"

            return ServiceStateResult(
                state=ContainerState.UPDATING,
                message=message,
                is_ready=False,
                container_counts=counts,
                error_pod=None,
            )

        # All running and ready
        if running >= desired_replicas:
            return ServiceStateResult(
                state=ContainerState.RUNNING,
                message="Healthy",
                is_ready=True,
                container_counts=counts,
                error_pod=None,
            )

        # Some starting (creating, health checks)
        if starting > 0:
            if running > 0:
                return ServiceStateResult(
                    state=ContainerState.STARTING,
                    message="Scaling up" if pending == 0 else "Starting",
                    is_ready=False,
                    container_counts=counts,
                    error_pod=None,
                )
            return ServiceStateResult(
                state=ContainerState.STARTING,
                message="Starting service",
                is_ready=False,
                container_counts=counts,
                error_pod=None,
            )

        # Some stopping (rolling update or scale down)
        if stopping > 0:
            if running > 0:
                return ServiceStateResult(
                    state=ContainerState.UPDATING,
                    message="Updating service",
                    is_ready=False,
                    container_counts=counts,
                    error_pod=None,
                )
            return ServiceStateResult(
                state=ContainerState.STOPPING,
                message="Shutting down",
                is_ready=False,
                container_counts=counts,
                error_pod=None,
            )

        # Pending
        if pending > 0:
            return ServiceStateResult(
                state=ContainerState.PENDING,
                message="Waiting to start",
                is_ready=False,
                container_counts=counts,
                error_pod=None,
            )

        # Some running but not all
        if running > 0:
            return ServiceStateResult(
                state=ContainerState.STARTING,
                message="Starting",
                is_ready=False,
                container_counts=counts,
                error_pod=None,
            )

        # Default to pending
        return ServiceStateResult(
            state=ContainerState.PENDING,
            message="Waiting",
            is_ready=False,
            container_counts=counts,
            error_pod=None,
        )

    def get_user_message(self, reason: str | None, raw_message: str | None) -> str:
        """Convert Kubernetes error messages to user-friendly messages."""
        if reason and reason in ERROR_MESSAGES:
            return ERROR_MESSAGES[reason]
        if raw_message:
            return raw_message
        if reason:
            return reason
        return "Unknown error"

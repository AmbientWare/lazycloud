from enum import StrEnum


class DeploymentStates(StrEnum):
    """Status of a deployment."""

    PENDING = "pending"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"

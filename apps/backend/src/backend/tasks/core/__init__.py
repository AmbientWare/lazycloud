"""Core task functions for deployment operations.

These are reusable async functions that perform the actual deployment work.
They are called by SAQ jobs and can be composed together.
"""

from backend.tasks.core.tasks import (
    check_deployment_idempotency,
    delete_existing_jobs,
    deploy_application,
    deploy_namespace_resources,
    prepare_deployment,
    prepare_namespace_config,
    reconcile_custom_domains_for_deployment,
    register_custom_domains,
    sync_deployment_to_db,
    unregister_custom_domains,
)
from backend.tasks.core.utils import (
    delete_job_with_timeout,
    update_deployment_state,
    verify_quota_capacity,
    wait_for_secrets,
)
from backend.tasks.core.schemas import DeploymentPreparationResult

__all__ = [
    # Task functions
    "check_deployment_idempotency",
    "prepare_deployment",
    "prepare_namespace_config",
    "deploy_namespace_resources",
    "delete_existing_jobs",
    "deploy_application",
    "reconcile_custom_domains_for_deployment",
    "register_custom_domains",
    "unregister_custom_domains",
    "sync_deployment_to_db",
    # Utilities
    "delete_job_with_timeout",
    "update_deployment_state",
    "verify_quota_capacity",
    "wait_for_secrets",
    # Schemas
    "DeploymentPreparationResult",
]

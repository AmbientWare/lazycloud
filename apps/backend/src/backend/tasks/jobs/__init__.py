"""SAQ job definitions for background tasks.

These jobs are triggered via API calls and executed by SAQ workers.
"""

from backend.tasks.jobs.deploy import deploy_compose_job
from backend.tasks.jobs.destroy import destroy_compose_job
from backend.tasks.jobs.rollback import rollback_compose_job
from backend.tasks.jobs.instances import delete_instance_job
from backend.tasks.jobs.services import restart_service_job, restart_all_services_job

BACKGROUND_JOBS = [
    deploy_compose_job,
    destroy_compose_job,
    rollback_compose_job,
    delete_instance_job,
    restart_service_job,
    restart_all_services_job,
]

__all__ = [
    "BACKGROUND_JOBS",
    "deploy_compose_job",
    "destroy_compose_job",
    "rollback_compose_job",
    "delete_instance_job",
    "restart_service_job",
    "restart_all_services_job",
]

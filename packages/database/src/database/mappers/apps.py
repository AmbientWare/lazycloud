from __future__ import annotations

from pydantic import JsonValue, TypeAdapter
from shared.app_lifecycle import (
    AppDeploymentIntentTarget,
    AppLifecycleState,
    AppLifecycleTarget,
)
from shared.cron import CronJobRecord
from shared.deployment_records import Deployment, DeploymentSpec
from shared.deployments import DeploymentKind, StubKind
from shared.placement import Placement
from shared.timestamps import to_utc, to_utc_or_none
from shared.workload_config import StubConfig

from database.records.apps import (
    AppContainerShutdownIntentRecord,
    AppDeploymentIntentRecord,
    AppRecord,
    StubRecord,
)
from database.tables.apps import (
    AppContainerShutdownIntentTable,
    AppDeploymentIntentTable,
    AppTable,
    CronJobTable,
    DeploymentTable,
    StubTable,
)

_CONFIG_DOCUMENT = TypeAdapter(dict[str, JsonValue])
_OBJECT_IDS = TypeAdapter(list[str])


def app_record_from_table(row: AppTable) -> AppRecord:
    return AppRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        stub_id=str(row.stub_id) if row.stub_id is not None else None,
        name=row.name,
        version=row.version,
        public=row.public,
        lifecycle_state=AppLifecycleState(row.lifecycle_state),
        lifecycle_revision=row.lifecycle_revision,
        lifecycle_target=(
            AppLifecycleTarget(row.lifecycle_target) if row.lifecycle_target is not None else None
        ),
        lifecycle_operation_id=(
            str(row.lifecycle_operation_id) if row.lifecycle_operation_id is not None else None
        ),
        lifecycle_failure=row.lifecycle_failure,
        reconcile_claim_id=(
            str(row.reconcile_claim_id) if row.reconcile_claim_id is not None else None
        ),
        reconcile_claimed_at=to_utc_or_none(row.reconcile_claimed_at),
        reconcile_attempt_count=row.reconcile_attempt_count,
        lifecycle_event_id=(
            str(row.lifecycle_event_id) if row.lifecycle_event_id is not None else None
        ),
        lifecycle_event_created_at=to_utc_or_none(row.lifecycle_event_created_at),
        lifecycle_change_published_at=to_utc_or_none(row.lifecycle_change_published_at),
        metadata=dict(row.metadata_json),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        deleted_at=to_utc_or_none(row.deleted_at),
    )


def write_app_row(row: AppTable, app: AppRecord) -> None:
    row.workspace_id = app.workspace_id
    row.stub_id = app.stub_id
    row.name = app.name
    row.version = app.version
    row.public = app.public
    row.lifecycle_state = app.lifecycle_state.value
    row.lifecycle_revision = app.lifecycle_revision
    row.lifecycle_target = app.lifecycle_target.value if app.lifecycle_target is not None else None
    row.lifecycle_operation_id = app.lifecycle_operation_id
    row.lifecycle_failure = app.lifecycle_failure
    row.reconcile_claim_id = app.reconcile_claim_id
    row.reconcile_claimed_at = app.reconcile_claimed_at
    row.reconcile_attempt_count = app.reconcile_attempt_count
    row.lifecycle_event_id = app.lifecycle_event_id
    row.lifecycle_event_created_at = app.lifecycle_event_created_at
    row.lifecycle_change_published_at = app.lifecycle_change_published_at
    row.metadata_json = dict(app.metadata)
    row.created_at = app.created_at
    row.updated_at = app.updated_at
    row.deleted_at = app.deleted_at


def deployment_from_table(row: DeploymentTable) -> Deployment:
    return Deployment(
        id=str(row.id),
        name=row.name,
        kind=DeploymentKind(row.kind),
        app_id=str(row.app_id) if row.app_id is not None else None,
        stub_id=str(row.stub_id) if row.stub_id is not None else None,
        version=row.version,
        spec=DeploymentSpec.model_validate(row.spec),
        subdomain=row.subdomain,
        custom_hostname=row.custom_hostname,
        placement=Placement.parse(row.placement),
        machine=row.machine,
        active=row.active,
        deleted_at=to_utc_or_none(row.deleted_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def write_deployment_row(row: DeploymentTable, deployment: Deployment) -> None:
    row.name = deployment.name
    row.kind = deployment.kind.value
    row.app_id = deployment.app_id
    row.stub_id = deployment.stub_id
    row.version = deployment.version
    row.spec = deployment.spec.model_dump(mode="json")
    row.subdomain = deployment.subdomain
    row.custom_hostname = deployment.custom_hostname
    row.placement = deployment.placement.key
    row.machine = deployment.machine
    row.active = deployment.active
    row.deleted_at = deployment.deleted_at
    row.created_at = deployment.created_at
    row.updated_at = deployment.updated_at


def cron_job_from_table(row: CronJobTable) -> CronJobRecord:
    return CronJobRecord(
        workspace_id=str(row.workspace_id),
        name=row.name,
        cron=row.cron,
        deployment_id=str(row.deployment_id),
        enabled=row.enabled,
        last_run_at=to_utc_or_none(row.last_run_at),
        next_run_at=to_utc_or_none(row.next_run_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def write_cron_job_row(row: CronJobTable, cron_job: CronJobRecord) -> None:
    row.name = cron_job.name
    row.cron = cron_job.cron
    row.deployment_id = cron_job.deployment_id
    row.enabled = cron_job.enabled
    row.last_run_at = cron_job.last_run_at
    row.next_run_at = cron_job.next_run_at
    row.created_at = cron_job.created_at
    row.updated_at = cron_job.updated_at


def app_deployment_intent_from_table(
    row: AppDeploymentIntentTable,
) -> AppDeploymentIntentRecord:
    return AppDeploymentIntentRecord(
        app_id=str(row.app_id),
        deployment_id=str(row.deployment_id),
        operation_revision=row.operation_revision,
        target=AppDeploymentIntentTarget(row.target),
        event_id=str(row.event_id) if row.event_id is not None else None,
        event_created_at=to_utc_or_none(row.event_created_at),
        workspace_change_published_at=to_utc_or_none(row.workspace_change_published_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def app_container_shutdown_intent_from_table(
    row: AppContainerShutdownIntentTable,
) -> AppContainerShutdownIntentRecord:
    return AppContainerShutdownIntentRecord(
        app_id=str(row.app_id),
        container_id=str(row.container_id),
        worker_id=row.worker_id,
        operation_revision=row.operation_revision,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


__all__ = [
    "app_container_shutdown_intent_from_table",
    "app_deployment_intent_from_table",
    "app_record_from_table",
    "write_app_row",
]


def stub_from_table(row: StubTable) -> StubRecord:
    config = dict(row.configuration)
    if row.object_id is not None:
        config["object_id"] = str(row.object_id)
    runtime_values: dict[str, JsonValue | list[str]] = {
        "region": row.runtime_region,
        "availability_zone": row.runtime_availability_zone,
        "cpu": row.runtime_cpu,
        "cpu_millicores": row.runtime_cpu_millicores,
        "memory": row.runtime_memory,
        "disk": row.runtime_disk,
        "memory_mib": row.runtime_memory_mib,
        "gpu": row.runtime_gpu,
        "gpu_count": row.runtime_gpu_count,
        "requires_gpu": row.runtime_requires_gpu,
        "image_id": row.runtime_image_id,
        "timeout_seconds": row.runtime_timeout_seconds,
        "retries": row.runtime_retries,
        "keep_warm": row.runtime_keep_warm,
        "concurrency": row.runtime_concurrency,
        "in_process": row.runtime_in_process,
        "workers": row.runtime_workers,
        "checkpoint_enabled": row.runtime_checkpoint_enabled,
        "checkpoint_readiness_path": row.runtime_checkpoint_readiness_path,
        "checkpoint_readiness_port": row.runtime_checkpoint_readiness_port,
        "checkpoint_readiness_timeout_seconds": row.runtime_checkpoint_readiness_timeout_seconds,
        "checkpoint_readiness_interval_seconds": row.runtime_checkpoint_readiness_interval_seconds,
        "health_check_path": row.runtime_health_check_path,
        "health_check_port": row.runtime_health_check_port,
        "runtime": row.runtime_runtime,
        "runtime_class": row.runtime_runtime_class,
        "docker_enabled": row.runtime_docker_enabled,
        "block_network": row.runtime_block_network,
        "allow_list": row.runtime_allow_list,
        "preemptible": row.runtime_preemptible,
        "workspace_gpu_quota": row.runtime_workspace_gpu_quota,
        "workspace_cpu_quota_millicores": row.runtime_workspace_cpu_quota_millicores,
    }
    runtime_document = config.get("runtime", {})
    if not isinstance(runtime_document, dict):
        raise ValueError("stub runtime configuration must be an object")
    runtime_document = dict(runtime_document)
    runtime_document.update(
        {
            key: value
            for key, value in _CONFIG_DOCUMENT.validate_python(runtime_values).items()
            if value is not None
        }
    )
    if runtime_document or "runtime" in config:
        config["runtime"] = runtime_document
    autoscaler: dict[str, JsonValue] = {
        "type": row.autoscaler_type,
        "max_containers": row.autoscaler_max_containers,
        "min_containers": row.autoscaler_min_containers,
        "tasks_per_container": row.autoscaler_tasks_per_container,
        "failed_container_threshold": row.autoscaler_failed_container_threshold,
        "max_failed_containers": row.autoscaler_max_failed_containers,
        "failure_threshold": row.autoscaler_failure_threshold,
        "failed_container_window_seconds": row.autoscaler_failed_container_window_seconds,
        "failure_window_seconds": row.autoscaler_failure_window_seconds,
    }
    autoscaler_document = config.get("autoscaler", {})
    if not isinstance(autoscaler_document, dict):
        raise ValueError("stub autoscaler configuration must be an object")
    autoscaler_document = dict(autoscaler_document)
    autoscaler_document.update(
        {key: value for key, value in autoscaler.items() if value is not None}
    )
    if autoscaler_document or "autoscaler" in config:
        config["autoscaler"] = autoscaler_document
    task_policy: dict[str, JsonValue] = {
        "timeout": row.task_policy_timeout,
        "timeout_seconds": row.task_policy_timeout_seconds,
        "ttl": row.task_policy_ttl,
        "ttl_seconds": row.task_policy_ttl_seconds,
    }
    task_policy_document = config.get("task_policy", {})
    if not isinstance(task_policy_document, dict):
        raise ValueError("stub task_policy configuration must be an object")
    task_policy_document = dict(task_policy_document)
    task_policy_document.update(
        {key: value for key, value in task_policy.items() if value is not None}
    )
    if task_policy_document or "task_policy" in config:
        config["task_policy"] = task_policy_document
    image = config.get("image", {})
    if not isinstance(image, dict):
        raise ValueError("stub image configuration must be an object")
    image = dict(image)
    if row.image_id is not None:
        image["image_id"] = row.image_id
    if row.image_context_object_id is not None:
        image["context_object_id"] = str(row.image_context_object_id)
    if image or "image" in config:
        config["image"] = image
    config_metadata = config.get("metadata", {})
    if not isinstance(config_metadata, dict):
        raise ValueError("stub configuration metadata must be an object")
    config_metadata = dict(config_metadata)
    if row.config_copied_object_ids is not None:
        config_metadata["copied_object_ids"] = list(row.config_copied_object_ids)
    if row.autoscaling_enabled is not None:
        config_metadata["autoscaling_enabled"] = row.autoscaling_enabled
    if config_metadata or "metadata" in config:
        config["metadata"] = config_metadata
    metadata = dict(row.metadata_json)
    if row.copied_object_ids is not None:
        metadata["copied_object_ids"] = list(row.copied_object_ids)
    return StubRecord(
        id=str(row.id),
        workspace_id=str(row.workspace_id),
        name=row.name,
        kind=StubKind(row.type),
        handler=row.handler,
        deployment_id=row.deployment_id,
        app_id=row.app_id,
        public=row.public,
        config=StubConfig.model_validate(config),
        metadata=metadata,
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
    )


def write_stub_row(row: StubTable, stub: StubRecord) -> None:
    row.workspace_id = stub.workspace_id
    row.name = stub.name
    row.type = stub.kind.value
    row.handler = stub.handler
    row.deployment_id = stub.deployment_id
    row.app_id = stub.app_id
    row.public = stub.public
    row.created_at = stub.created_at
    row.updated_at = stub.updated_at
    row.object_id = stub.config.object_id or None
    row.image_id = stub.config.image.image_id
    row.image_context_object_id = stub.config.image.context_object_id or None
    config = _CONFIG_DOCUMENT.validate_json(
        stub.config.model_dump_json(exclude_unset=True, by_alias=True)
    )
    config.pop("object_id", None)
    row.runtime_region = (
        stub.config.runtime.region if "region" in stub.config.runtime.model_fields_set else None
    )
    row.runtime_availability_zone = (
        stub.config.runtime.availability_zone
        if "availability_zone" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_cpu = (
        _CONFIG_DOCUMENT.validate_json(stub.config.runtime.model_dump_json()).get("cpu")
        if "cpu" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_cpu_millicores = (
        stub.config.runtime.cpu_millicores
        if "cpu_millicores" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_memory = (
        _CONFIG_DOCUMENT.validate_json(stub.config.runtime.model_dump_json()).get("memory")
        if "memory" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_disk = (
        _CONFIG_DOCUMENT.validate_json(stub.config.runtime.model_dump_json()).get("disk")
        if "disk" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_memory_mib = (
        stub.config.runtime.memory_mib
        if "memory_mib" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_gpu = (
        stub.config.runtime.gpu if "gpu" in stub.config.runtime.model_fields_set else None
    )
    row.runtime_gpu_count = (
        stub.config.runtime.gpu_count
        if "gpu_count" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_requires_gpu = (
        stub.config.runtime.requires_gpu
        if "requires_gpu" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_image_id = (
        stub.config.runtime.image_id if "image_id" in stub.config.runtime.model_fields_set else None
    )
    row.runtime_timeout_seconds = (
        stub.config.runtime.timeout_seconds
        if "timeout_seconds" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_retries = (
        stub.config.runtime.retries if "retries" in stub.config.runtime.model_fields_set else None
    )
    row.runtime_keep_warm = (
        stub.config.runtime.keep_warm
        if "keep_warm" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_concurrency = (
        stub.config.runtime.concurrency
        if "concurrency" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_in_process = (
        stub.config.runtime.in_process
        if "in_process" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_workers = (
        stub.config.runtime.workers if "workers" in stub.config.runtime.model_fields_set else None
    )
    row.runtime_checkpoint_enabled = (
        stub.config.runtime.checkpoint_enabled
        if "checkpoint_enabled" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_checkpoint_readiness_path = (
        stub.config.runtime.checkpoint_readiness_path
        if "checkpoint_readiness_path" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_checkpoint_readiness_port = (
        stub.config.runtime.checkpoint_readiness_port
        if "checkpoint_readiness_port" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_checkpoint_readiness_timeout_seconds = (
        stub.config.runtime.checkpoint_readiness_timeout_seconds
        if "checkpoint_readiness_timeout_seconds" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_checkpoint_readiness_interval_seconds = (
        stub.config.runtime.checkpoint_readiness_interval_seconds
        if "checkpoint_readiness_interval_seconds" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_health_check_path = (
        stub.config.runtime.health_check_path
        if "health_check_path" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_health_check_port = (
        stub.config.runtime.health_check_port
        if "health_check_port" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_runtime = (
        stub.config.runtime.runtime if "runtime" in stub.config.runtime.model_fields_set else None
    )
    row.runtime_runtime_class = (
        stub.config.runtime.runtime_class
        if "runtime_class" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_docker_enabled = (
        stub.config.runtime.docker_enabled
        if "docker_enabled" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_block_network = (
        stub.config.runtime.block_network
        if "block_network" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_allow_list = (
        stub.config.runtime.allow_list
        if "allow_list" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_preemptible = (
        stub.config.runtime.preemptible
        if "preemptible" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_workspace_gpu_quota = (
        stub.config.runtime.workspace_gpu_quota
        if "workspace_gpu_quota" in stub.config.runtime.model_fields_set
        else None
    )
    row.runtime_workspace_cpu_quota_millicores = (
        stub.config.runtime.workspace_cpu_quota_millicores
        if "workspace_cpu_quota_millicores" in stub.config.runtime.model_fields_set
        else None
    )
    runtime_document = config.get("runtime")
    if isinstance(runtime_document, dict):
        config["runtime"] = {
            key: value
            for key, value in runtime_document.items()
            if key
            not in {
                "memory",
                "concurrency",
                "availability_zone",
                "runtime_class",
                "workspace_gpu_quota",
                "allow_list",
                "keep_warm",
                "timeout_seconds",
                "workers",
                "cpu_millicores",
                "gpu_count",
                "checkpoint_readiness_interval_seconds",
                "checkpoint_readiness_path",
                "region",
                "memory_mib",
                "workspace_cpu_quota_millicores",
                "health_check_port",
                "cpu",
                "gpu",
                "disk",
                "retries",
                "in_process",
                "requires_gpu",
                "checkpoint_readiness_port",
                "health_check_path",
                "checkpoint_readiness_timeout_seconds",
                "checkpoint_enabled",
                "image_id",
                "preemptible",
                "docker_enabled",
                "block_network",
                "runtime",
            }
        }
    row.autoscaler_type = (
        stub.config.autoscaler.type if "type" in stub.config.autoscaler.model_fields_set else None
    )
    row.autoscaler_max_containers = (
        stub.config.autoscaler.max_containers
        if "max_containers" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_min_containers = (
        stub.config.autoscaler.min_containers
        if "min_containers" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_tasks_per_container = (
        stub.config.autoscaler.tasks_per_container
        if "tasks_per_container" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_failed_container_threshold = (
        stub.config.autoscaler.failed_container_threshold
        if "failed_container_threshold" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_max_failed_containers = (
        stub.config.autoscaler.max_failed_containers
        if "max_failed_containers" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_failure_threshold = (
        stub.config.autoscaler.failure_threshold
        if "failure_threshold" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_failed_container_window_seconds = (
        stub.config.autoscaler.failed_container_window_seconds
        if "failed_container_window_seconds" in stub.config.autoscaler.model_fields_set
        else None
    )
    row.autoscaler_failure_window_seconds = (
        stub.config.autoscaler.failure_window_seconds
        if "failure_window_seconds" in stub.config.autoscaler.model_fields_set
        else None
    )
    autoscaler_document = config.get("autoscaler")
    if isinstance(autoscaler_document, dict):
        config["autoscaler"] = {
            key: value
            for key, value in autoscaler_document.items()
            if key
            not in {
                "failed_container_window_seconds",
                "max_containers",
                "min_containers",
                "tasks_per_container",
                "max_failed_containers",
                "failure_threshold",
                "failed_container_threshold",
                "failure_window_seconds",
                "type",
            }
        }
    row.task_policy_timeout = (
        stub.config.task_policy.timeout
        if "timeout" in stub.config.task_policy.model_fields_set
        else None
    )
    row.task_policy_timeout_seconds = (
        stub.config.task_policy.timeout_seconds
        if "timeout_seconds" in stub.config.task_policy.model_fields_set
        else None
    )
    row.task_policy_ttl = (
        stub.config.task_policy.ttl if "ttl" in stub.config.task_policy.model_fields_set else None
    )
    row.task_policy_ttl_seconds = (
        stub.config.task_policy.ttl_seconds
        if "ttl_seconds" in stub.config.task_policy.model_fields_set
        else None
    )
    task_policy_document = config.get("task_policy")
    if isinstance(task_policy_document, dict):
        config["task_policy"] = {
            key: value
            for key, value in task_policy_document.items()
            if key not in {"ttl_seconds", "ttl", "timeout_seconds", "timeout"}
        }
    image = config.get("image")
    if isinstance(image, dict):
        config["image"] = {
            key: value
            for key, value in image.items()
            if key not in {"image_id", "context_object_id"}
        }
    row.metadata_json = dict(stub.metadata)
    copied = row.metadata_json.pop("copied_object_ids", None)
    row.copied_object_ids = _OBJECT_IDS.validate_python(copied) if copied is not None else None
    config_metadata = config.get("metadata")
    row.config_copied_object_ids = None
    row.autoscaling_enabled = None
    if isinstance(config_metadata, dict):
        copied = config_metadata.pop("copied_object_ids", None)
        row.config_copied_object_ids = (
            _OBJECT_IDS.validate_python(copied) if copied is not None else None
        )
        enabled = config_metadata.pop("autoscaling_enabled", None)
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("autoscaling_enabled must be a boolean")
        row.autoscaling_enabled = enabled
    row.configuration = config

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

import pytest
import yaml
from pydantic import JsonValue, TypeAdapter
from shared.app_identity import (
    CONTROL_PLANE_SERVICE_NAME,
)

ROOT = Path(__file__).resolve().parents[2]

type PathPart = str | int
type YamlMapping = dict[str, JsonValue]

_YAML_MAPPING_ADAPTER = TypeAdapter(dict[str, JsonValue])
_JSON_MAPPING_SEQUENCE_ADAPTER = TypeAdapter(list[dict[str, JsonValue]])


def test_helm_chart_rejects_worker_name_that_cannot_preserve_pool_identity() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "a" * 53,
            str(ROOT / "deploy/charts/lazycloud"),
            "--values",
            str(ROOT / "deploy/charts/lazycloud/values.local.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "truncate worker pool identity" in result.stdout + result.stderr


def test_helm_chart_rejects_inverted_cache_disk_watermarks() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "invalid-cache-policy",
            str(ROOT / "deploy/charts/lazycloud"),
            "--values",
            str(ROOT / "deploy/charts/lazycloud/values.local.yaml"),
            "--set-json",
            "cache.diskMaxUsagePct=0.80",
            "--set-json",
            "cache.diskEvictWatermarkPct=0.90",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "cache disk watermarks must satisfy 0 < evict <= max <= 1" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("controlPlane.publicHttpUrl", "ftp://control.example.test"),
        ("controlPlane.publicHttpUrl", "https://user:secret@control.example.test"),
        ("controlPlane.publicHttpUrl", "https://control.example.test/api"),
        ("controlPlane.publicHttpUrl", "https://control.example.test?query=one"),
        ("controlPlane.publicHttpUrl", "https://control.example.test#fragment"),
        ("controlPlane.publicHttpUrl", "https://"),
        ("controlPlane.publicHttpUrl", "https://control.example.test:99999"),
        ("controlPlane.runtimeHttpUrl", "http://release-lazycloud:9000/callback"),
    ],
)
def test_helm_chart_rejects_non_origin_gateway_urls(field: str, value: str) -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "invalid-origin",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://control.example.test",
            "--set-string",
            f"{field}={value}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert field.rsplit(".", 1)[-1] in result.stdout + result.stderr


def test_helm_local_render_contains_required_services_and_secure_pods() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    result = subprocess.run(
        [
            helm,
            "template",
            "lazycloud",
            str(ROOT / "deploy/charts/lazycloud"),
            "--values",
            str(ROOT / "deploy/charts/lazycloud/values.local.yaml"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "local Helm render")
    names = _names_by_kind(resources)

    assert {
        ("StatefulSet", "lazycloud-postgresql"),
        ("StatefulSet", "lazycloud-redis"),
        ("Deployment", "lazycloud-object-store"),
        ("Deployment", "lazycloud-juicefs-gateway"),
        ("Deployment", "lazycloud-container-worker-default"),
    } <= names
    for component in (
        "database-bootstrap",
        "administrator-bootstrap",
        "object-store-init",
        "worker-token-bootstrap",
    ):
        _resource_by_component(resources, "Job", component)
    cache = _resource(resources, "DaemonSet", "lazycloud-cache")
    assert _value_path(cache, "spec", "template", "spec", "automountServiceAccountToken") is False

    storage = _resource(resources, "Deployment", "lazycloud-juicefs-gateway")
    storage_pod = _mapping_path(storage, "spec", "template", "spec")
    assert _value_path(storage_pod, "automountServiceAccountToken") is False
    format_container = _mapping_path(storage_pod, "initContainers", 0)
    storage_containers = {
        _string_path(container, "name"): container
        for container in _mapping_sequence_path(storage_pod, "containers")
    }
    assert _string_path(format_container, "image") == "storage-gateway:local"
    assert {
        name: _string_path(storage_containers[name], "image")
        for name in ("juicefs-gateway", "juicefs-webdav")
    } == {
        "juicefs-gateway": "storage-gateway:local",
        "juicefs-webdav": "storage-gateway:local",
    }

    worker = _resource(resources, "Deployment", "lazycloud-container-worker-default")
    assert (
        _string_path(worker, "spec", "template", "spec", "containers", 0, "image")
        == "container-worker:local"
    )


def test_helm_aws_capacity_uses_provider_identity_and_secret_refs() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    template_sha256 = "a" * 64
    agent_sha256 = "b" * 64
    worker_digest = f"registry.example.test/lazycloud/worker@sha256:{'c' * 64}"
    result = subprocess.run(
        [
            helm,
            "template",
            "customer",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://control.example.test",
            "--set",
            "awsCapacity.enabled=true",
            "--set-string",
            "awsCapacity.connectionTemplateUrl="
            "https://s3.us-east-1.amazonaws.com/release-assets-example/connected-aws/"
            f"connection-templates/{template_sha256}/template.json",
            "--set-string",
            "agentArtifact.version=0.1.0",
            "--set-string",
            f"agentArtifact.sha256ByArch.amd64={agent_sha256}",
            "--set-string",
            "agentArtifact.url=https://s3.us-east-1.amazonaws.com/release-assets-example/"
            f"connected-aws/agents/0.1.0/{agent_sha256}/lazycloud-agent-linux-amd64",
            "--set-string",
            f"awsCapacity.workerImageDigest={worker_digest}",
            "--set-json",
            'awsCapacity.cpuAmiIds={"us-east-1":"ami-0123456789abcdef0"}',
            "--set-json",
            'awsCapacity.gpuAmiIds={"us-east-1":"ami-0fedcba9876543210"}',
            "--set-string",
            "awsCapacity.controlPrincipalArn=arn:aws:iam::123456789012:role/control",
            "--set-json",
            'awsCapacity.instanceHourlyMicros={"i4i.xlarge":340000}',
            "--set",
            "tailnet.enabled=true",
            "--set-string",
            "tailnet.authKeySecretRef.name=customer-tailnet",
            "--set-string",
            "tailnet.oauthClientId=oauth-client-id",
            "--set-string",
            "tailnet.oauthClientSecretRef.name=customer-tailnet",
            "--set-string",
            "tailnet.routeAuthKeySecretRef.name=customer-tailnet",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "AWS capacity Helm render")
    control = _resource(resources, "Deployment", "customer-lazycloud-control-plane")
    pod = _mapping_path(control, "spec", "template", "spec")
    env = _env_by_name(_mapping_path(pod, "containers", 0))

    assert _string_path(pod, "serviceAccountName") == "customer-lazycloud-provider"
    assert _string_path(env["LAZYCLOUD_AWS_CONNECTION_ENABLED"], "value") == "true"
    assert _string_path(env["LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST"], "value") == worker_digest
    assert (
        _mapping_path(env["LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY"], "valueFrom", "secretKeyRef")["name"]
        == "customer-tailnet"
    )
    assert "LAZYCLOUD_TAILNET_AUTH_KEY" not in env


def test_helm_database_bootstrap_uses_the_current_schema_only() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "current",
            str(ROOT / "deploy/charts/lazycloud"),
            "--values",
            str(ROOT / "deploy/charts/lazycloud/values.local.yaml"),
            "--is-upgrade",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "current schema bootstrap render")
    bootstrap = _resource_by_component(resources, "Job", "database-bootstrap")
    metadata = _mapping_path(bootstrap, "metadata")
    if "annotations" in metadata:
        assert "helm.sh/hook" not in _mapping(
            metadata["annotations"],
            "database bootstrap Job annotations",
        )
    bootstrap_hash = _string_path(
        bootstrap,
        "metadata",
        "labels",
        "lazycloud.io/job-spec-hash",
    )
    assert len(bootstrap_hash) == 12
    assert _string_path(bootstrap, "metadata", "name").endswith(f"-{bootstrap_hash}")
    assert _value_path(bootstrap, "spec", "ttlSecondsAfterFinished") == 600
    pod = _mapping_path(bootstrap, "spec", "template", "spec")
    assert _value_path(pod, "automountServiceAccountToken") is False
    container = _mapping_path(pod, "containers", 0)
    assert _sequence_path(container, "command") == [
        "lazycloud-admin",
        "--json",
        "database",
        "initialize",
    ]
    bootstrap_components = {
        _string_path(resource, "metadata", "name")
        for resource in resources
        if _string_path(resource, "kind") in {"ServiceAccount", "Role", "RoleBinding"}
        and "database-bootstrap" in _string_path(resource, "metadata", "name")
    }
    assert bootstrap_components == set()
    assert "backup" not in result.stdout.lower()
    assert "database upgrade" not in result.stdout
    assert "database restore" not in result.stdout


def test_helm_one_shot_jobs_change_identity_with_their_immutable_specs() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    chart = str(ROOT / "deploy/charts/lazycloud")
    local_values = str(ROOT / "deploy/charts/lazycloud/values.local.yaml")

    def render(*extra: str) -> list[YamlMapping]:
        result = subprocess.run(
            [
                helm,
                "template",
                "one-shot",
                chart,
                "--values",
                local_values,
                *extra,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return _yaml_documents_from_text(result.stdout, "one-shot Job render")

    installed = render()
    unchanged_upgrade = render("--is-upgrade")
    changed_upgrade = render(
        "--is-upgrade",
        "--set-string",
        "objectStore.bucketInitImage=amazon/aws-cli:object-change",
        "--set-string",
        "containerWorkers.workerTokenBootstrap.image.tag=token-change",
        "--set-string",
        "databaseBootstrap.image.tag=database-change",
        "--set-string",
        "administratorBootstrap.image.tag=administrator-change",
    )
    components = (
        "administrator-bootstrap",
        "database-bootstrap",
        "object-store-init",
        "worker-token-bootstrap",
    )
    changed_job_names = {
        _string_path(resource, "metadata", "name")
        for resource in changed_upgrade
        if _string_path(resource, "kind") == "Job"
    }
    for component in components:
        installed_job = _resource_by_component(installed, "Job", component)
        unchanged_job = _resource_by_component(unchanged_upgrade, "Job", component)
        changed_job = _resource_by_component(changed_upgrade, "Job", component)
        installed_name = _string_path(installed_job, "metadata", "name")
        unchanged_name = _string_path(unchanged_job, "metadata", "name")
        changed_name = _string_path(changed_job, "metadata", "name")
        installed_hash = _string_path(
            installed_job,
            "metadata",
            "labels",
            "lazycloud.io/job-spec-hash",
        )

        assert installed_name == unchanged_name
        assert installed_name != changed_name
        assert installed_name not in changed_job_names
        assert installed_name.startswith("one-shot-lazycloud-")
        assert len(installed_name) <= 63
        assert len(installed_hash) == 12
        assert all(character in "0123456789abcdef" for character in installed_hash)
        assert installed_name.endswith(f"-{installed_hash}")
        assert (
            _string_path(
                installed_job,
                "metadata",
                "labels",
                "app.kubernetes.io/instance",
            )
            == "one-shot"
        )
        assert (
            _string_path(
                installed_job,
                "metadata",
                "labels",
                "app.kubernetes.io/managed-by",
            )
            == "Helm"
        )
        assert _value_path(installed_job, "spec", "ttlSecondsAfterFinished") == 600
        assert _mapping_path(installed_job, "spec", "template") == _mapping_path(
            unchanged_job,
            "spec",
            "template",
        )
        assert _mapping_path(installed_job, "spec", "template") != _mapping_path(
            changed_job,
            "spec",
            "template",
        )
        metadata = _mapping_path(installed_job, "metadata")
        if "annotations" in metadata:
            assert "helm.sh/hook" not in _mapping(
                metadata["annotations"],
                f"{component} Job annotations",
            )


def test_helm_chart_can_resolve_a_separate_image_archive_backend() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "archive",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://archive.example.test",
            "--set",
            "objectStore.endpointUrl=https://objects.example.test",
            "--set",
            "imageBuild.archive.backend.enabled=true",
            "--set",
            "imageBuild.archive.backend.bucket=image-archives",
            "--set",
            "imageBuild.archive.backend.endpointUrl=https://archives.example.test",
            "--set",
            "imageBuild.archive.backend.presignedEndpointUrl=https://downloads.example.test",
            "--set",
            "imageBuild.archive.backend.regionName=eu-test-1",
            "--set",
            "imageBuild.archive.backend.transferMaxConcurrency=4",
            "--set",
            "imageBuild.archive.backend.credentialsSecretRef.name=archive-storage",
            "--set",
            "imageBuild.archive.backend.credentialsSecretRef.accessKeyIdKey=access-key",
            "--set",
            "imageBuild.archive.backend.credentialsSecretRef.secretAccessKeyKey=secret-key",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "helm template archive")
    control = _mapping_path(
        _resource(resources, "Deployment", "archive-lazycloud-control-plane"),
        "spec",
        "template",
        "spec",
        "containers",
        0,
    )
    scheduler = _mapping_path(
        _resource(resources, "Deployment", "archive-lazycloud-scheduler"),
        "spec",
        "template",
        "spec",
        "containers",
        0,
    )
    container_worker = _mapping_path(
        _resource(resources, "Deployment", "archive-lazycloud-container-worker-default"),
        "spec",
        "template",
        "spec",
        "containers",
        0,
    )

    for process in (control, scheduler):
        env = _env_by_name(process)
        assert _string_path(env["LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL"], "value") == (
            "https://objects.example.test"
        )
        assert _string_path(env["LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__BUCKET"], "value") == (
            "image-archives"
        )
        assert _string_path(env["LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__ENDPOINT_URL"], "value") == (
            "https://archives.example.test"
        )
        assert _mapping_path(
            env["LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__SECRET_ACCESS_KEY"],
            "valueFrom",
            "secretKeyRef",
        ) == {"name": "archive-storage", "key": "secret-key", "optional": False}

    worker_env = _env_by_name(container_worker)
    assert not {
        "WORKER_IMAGE_ARCHIVE_BUCKET",
        "WORKER_IMAGE_ARCHIVE_ENDPOINT_URL",
        "WORKER_IMAGE_ARCHIVE_REGION_NAME",
        "WORKER_IMAGE_ARCHIVE_FORCE_PATH_STYLE",
        "WORKER_IMAGE_ARCHIVE_ACCESS_KEY_ID",
        "WORKER_IMAGE_ARCHIVE_SECRET_ACCESS_KEY",
        "WORKER_IMAGE_ARCHIVE_TRANSFER_MULTIPART_THRESHOLD_BYTES",
        "WORKER_IMAGE_ARCHIVE_TRANSFER_MULTIPART_CHUNK_SIZE_BYTES",
        "WORKER_IMAGE_ARCHIVE_TRANSFER_MAX_CONCURRENCY",
        "WORKER_IMAGE_ARCHIVE_PREFIX",
        "LAZYCLOUD_IMAGE_ARCHIVE_PRESIGNED_ENDPOINT_URL",
    }.intersection(worker_env)


def test_helm_chart_isolates_two_local_releases() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    releases: dict[str, list[YamlMapping]] = {}
    for release in ("alpha", "beta"):
        result = subprocess.run(
            [
                helm,
                "template",
                release,
                str(ROOT / "deploy/charts/lazycloud"),
                "--namespace",
                "shared",
                "--values",
                str(ROOT / "deploy/charts/lazycloud/values.local.yaml"),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        releases[release] = _yaml_documents_from_text(result.stdout, f"helm template {release}")

    alpha_names = _names_by_kind(releases["alpha"])
    beta_names = _names_by_kind(releases["beta"])
    assert alpha_names.isdisjoint(beta_names)

    for release, resources in releases.items():
        fullname = f"{release}-lazycloud"
        config = _resource(resources, "Secret", f"{fullname}-config")
        string_data = _string_mapping_path(config, "stringData")
        assert string_data["LAZYCLOUD_DATABASE_URL"].endswith(
            f"@{fullname}-postgresql:5432/lazycloud"
        )
        assert string_data["LAZYCLOUD_REDIS_URL"] == f"redis://{fullname}-redis:6379/0"
        control_plane = _resource(resources, "Deployment", f"{fullname}-control-plane")
        env = _env_by_name(
            _mapping_path(control_plane, "spec", "template", "spec", "containers", 0)
        )
        assert _string_path(env["LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL"], "value") == (
            f"http://{fullname}-object-store:9000"
        )


def test_helm_default_requires_external_configuration_without_rendering_secrets() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    missing_public_origin = subprocess.run(
        [helm, "template", "production", str(ROOT / "deploy/charts/lazycloud")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_public_origin.returncode != 0
    assert "controlPlane.publicHttpUrl" in (
        missing_public_origin.stdout + missing_public_origin.stderr
    )

    result = subprocess.run(
        [
            helm,
            "template",
            "production",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://control.example.test",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "helm template production")
    names_by_kind = _names_by_kind(resources)

    assert ("Secret", "production-lazycloud-config") not in names_by_kind
    assert ("Secret", "production-lazycloud-cache-service") not in names_by_kind
    assert not any(kind == "StatefulSet" for kind, _name in names_by_kind)
    assert ("Deployment", "production-lazycloud-object-store") not in names_by_kind
    control_plane = _resource(resources, "Deployment", "production-lazycloud-control-plane")
    control_container = _mapping_path(control_plane, "spec", "template", "spec", "containers", 0)
    assert "envFrom" not in control_container
    control_env = _env_by_name(control_container)
    assert _mapping_path(control_env["LAZYCLOUD_DATABASE_URL"], "valueFrom", "secretKeyRef") == {
        "name": "production-lazycloud-config",
        "key": "LAZYCLOUD_DATABASE_URL",
        "optional": False,
    }
    assert _string_path(control_env["LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL"], "value") == (
        "https://s3.amazonaws.com"
    )
    cache = _resource(resources, "DaemonSet", "production-lazycloud-cache")
    cache_container = _mapping_path(cache, "spec", "template", "spec", "containers", 0)
    cache_env = _env_by_name(cache_container)
    assert _mapping_path(
        cache_env["LAZYCLOUD_CACHE_SERVICE_TOKEN"],
        "valueFrom",
        "secretKeyRef",
    ) == {
        "name": "production-lazycloud-cache-service",
        "key": "token",
        "optional": False,
    }


def test_helm_secret_rotation_changes_long_running_pod_templates() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    chart = str(ROOT / "deploy/charts/lazycloud")
    local_values = str(ROOT / "deploy/charts/lazycloud/values.local.yaml")
    local_renders: list[list[YamlMapping]] = []
    for secret_value in ("local-secret-a", "local-secret-b"):
        result = subprocess.run(
            [
                helm,
                "template",
                "owned",
                chart,
                "--values",
                local_values,
                "--set-string",
                f"objectStore.secretAccessKey={secret_value}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        local_renders.append(_yaml_documents_from_text(result.stdout, "owned Secret render"))

    owned_annotations = [
        _mapping_path(
            _resource(resources, "Deployment", "owned-lazycloud-control-plane"),
            "spec",
            "template",
            "metadata",
            "annotations",
        )
        for resources in local_renders
    ]
    assert (
        owned_annotations[0]["checksum/lazycloud-config"]
        != owned_annotations[1]["checksum/lazycloud-config"]
    )

    external_renders: list[list[YamlMapping]] = []
    for revision in ("revision-a", "revision-b"):
        result = subprocess.run(
            [
                helm,
                "template",
                "external",
                chart,
                "--set-string",
                "controlPlane.publicHttpUrl=https://control.example.test",
                "--set-string",
                f"config.externalSecretRevision={revision}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        external_renders.append(
            _yaml_documents_from_text(result.stdout, "external Secret revision render")
        )

    for index, revision in enumerate(("revision-a", "revision-b")):
        resources = external_renders[index]
        for kind, name in (
            ("Deployment", "external-lazycloud-control-plane"),
            ("Deployment", "external-lazycloud-scheduler"),
            ("Deployment", "external-lazycloud-container-worker-default"),
            ("DaemonSet", "external-lazycloud-cache"),
        ):
            annotations = _mapping_path(
                _resource(resources, kind, name),
                "spec",
                "template",
                "metadata",
                "annotations",
            )
            assert annotations["lazycloud.io/external-secret-revision"] == revision
            assert "checksum/lazycloud-config" not in annotations


def test_helm_chart_derives_scaler_and_rbac_from_bounded_pool_policy() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    pool = _default_container_worker_pool()
    pool["scalingEnabled"] = False
    result = subprocess.run(
        [
            helm,
            "template",
            "bounded",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://control.example.test",
            "--set-json",
            f"containerWorkers.pools={json.dumps([pool])}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "non-scaling pool policy")
    scheduler = _resource(resources, "Deployment", "bounded-lazycloud-scheduler")
    scheduler_env = _env_by_name(
        _mapping_path(scheduler, "spec", "template", "spec", "containers", 0)
    )

    assert "LAZYCLOUD_WORKER_POOL_SCALER" not in scheduler_env
    assert "LAZYCLOUD_KUBERNETES_CAPACITY_POOLS" in scheduler_env
    assert not any(
        _string_path(resource, "kind") == "Role"
        and _string_path(resource, "metadata", "name")
        == "bounded-lazycloud-provider-container-worker-scale"
        for resource in resources
    )


def test_helm_chart_keeps_advertised_gpu_and_pod_resources_exact() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    pool = _default_container_worker_pool()
    gpu = _mapping(pool["gpu"], "default worker pool GPU policy")
    gpu["enabled"] = True
    gpu["count"] = 2
    gpu["type"] = "L4"
    pool["gpu"] = gpu
    result = subprocess.run(
        [
            helm,
            "template",
            "gpu",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://control.example.test",
            "--set-json",
            f"containerWorkers.pools={json.dumps([pool])}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "GPU worker policy")
    deployment = _resource(resources, "Deployment", "gpu-lazycloud-container-worker-default")
    worker = _mapping_path(deployment, "spec", "template", "spec", "containers", 0)
    assert _mapping_path(worker, "resources") == {
        "requests": {"cpu": "4000m", "memory": "8192Mi", "nvidia.com/gpu": "2"},
        "limits": {"cpu": "4000m", "memory": "8192Mi", "nvidia.com/gpu": "2"},
    }
    config = _resource(resources, "ConfigMap", "gpu-lazycloud-container-worker-default-config")
    document = _mapping(
        _YAML_MAPPING_ADAPTER.validate_python(
            yaml.safe_load(_string_path(config, "data", "worker.yaml"))
        ),
        "GPU worker configuration",
    )
    execution = _mapping_path(document, "configuration", "execution")
    assert _mapping(execution["capacity"], "GPU worker capacity") == {
        "cpu_millicores": 4000,
        "memory_mib": 8192,
        "gpu_type": "L4",
        "gpu_count": 2,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            [
                (("initialWorkers",), 3),
                (("maxWorkers",), 2),
            ],
            "minWorkers <= initialWorkers <= maxWorkers",
        ),
        (
            [
                (("gpu", "enabled"), False),
                (("gpu", "count"), 1),
            ],
            "must advertise zero GPUs",
        ),
    ],
)
def test_helm_chart_rejects_incoherent_worker_pool_policy(
    overrides: list[tuple[tuple[str, ...], JsonValue]],
    message: str,
) -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    pool = _default_container_worker_pool()
    for path, value in overrides:
        _set_mapping_path(pool, path, value)
    command = [
        helm,
        "template",
        "invalid-policy",
        str(ROOT / "deploy/charts/lazycloud"),
        "--set-string",
        "controlPlane.publicHttpUrl=https://control.example.test",
        "--set-json",
        f"containerWorkers.pools={json.dumps([pool])}",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)

    assert result.returncode != 0
    assert message in result.stdout + result.stderr


def test_helm_chart_renders_platform_autoscaling_and_disruption_controls() -> None:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")

    result = subprocess.run(
        [
            helm,
            "template",
            "lazycloud",
            str(ROOT / "deploy/charts/lazycloud"),
            "--set-string",
            "controlPlane.publicHttpUrl=https://control.example.test",
            "--set",
            "monitoring.enabled=true",
            "--set",
            "controlPlane.autoscaling.enabled=true",
            "--set",
            "controlPlane.podDisruptionBudget.enabled=true",
            "--set",
            "scheduler.autoscaling.enabled=true",
            "--set",
            "scheduler.podDisruptionBudget.enabled=true",
            "--set",
            "cache.podDisruptionBudget.enabled=true",
            "--set",
            "monitoring.metrics.autoscaling.enabled=true",
            "--set",
            "monitoring.metrics.podDisruptionBudget.enabled=true",
            "--set",
            "monitoring.agent.autoscaling.enabled=true",
            "--set",
            "monitoring.agent.podDisruptionBudget.enabled=true",
            "--set",
            "controlPlane.topologySpreadConstraints[0].maxSkew=1",
            "--set",
            "controlPlane.topologySpreadConstraints[0].topologyKey=kubernetes.io/hostname",
            "--set",
            "controlPlane.topologySpreadConstraints[0].whenUnsatisfiable=ScheduleAnyway",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    resources = _yaml_documents_from_text(result.stdout, "helm template autoscaling")
    names_by_kind = _names_by_kind(resources)

    for name in [
        "lazycloud-control-plane",
        "lazycloud-scheduler",
        "lazycloud-metrics",
        "lazycloud-metrics-agent",
    ]:
        assert ("HorizontalPodAutoscaler", name) in names_by_kind
        assert ("PodDisruptionBudget", name) in names_by_kind

    assert ("PodDisruptionBudget", "lazycloud-cache") in names_by_kind
    assert ("HorizontalPodAutoscaler", "lazycloud-cache") not in names_by_kind
    assert ("HorizontalPodAutoscaler", "lazycloud-container-worker-default") not in names_by_kind

    control_plane = _resource(resources, "Deployment", "lazycloud-control-plane")
    control_plane_container = _mapping_path(
        control_plane, "spec", "template", "spec", "containers", 0
    )
    assert _sequence_path(control_plane_container, "args") == [
        CONTROL_PLANE_SERVICE_NAME,
        "--host",
        "0.0.0.0",
        "--port",
        "9000",
    ]
    assert "replicas" not in _mapping_path(control_plane, "spec")
    assert (
        _value_path(control_plane, "spec", "template", "spec", "terminationGracePeriodSeconds")
        == 60
    )
    assert _mapping_path(
        control_plane, "spec", "template", "spec", "topologySpreadConstraints", 0
    ) == {
        "maxSkew": 1,
        "topologyKey": "kubernetes.io/hostname",
        "whenUnsatisfiable": "ScheduleAnyway",
    }

    scheduler = _resource(resources, "Deployment", "lazycloud-scheduler")
    scheduler_container = _mapping_path(scheduler, "spec", "template", "spec", "containers", 0)
    assert "replicas" not in _mapping_path(scheduler, "spec")
    scheduler_probe_command = [
        "python",
        "-m",
        "shared.process_liveness",
        "/tmp/lazycloud-scheduler.heartbeat",
        "--max-age-seconds",
        "120",
    ]
    assert (
        _sequence_path(scheduler_container, "readinessProbe", "exec", "command")
        == scheduler_probe_command
    )
    assert (
        _sequence_path(scheduler_container, "livenessProbe", "exec", "command")
        == scheduler_probe_command
    )

    hpa = _resource(resources, "HorizontalPodAutoscaler", "lazycloud-control-plane")
    assert _mapping_path(hpa, "spec", "scaleTargetRef") == {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "name": "lazycloud-control-plane",
    }
    assert _value_path(hpa, "spec", "minReplicas") == 2
    assert _value_path(hpa, "spec", "maxReplicas") == 10
    assert _value_path(hpa, "spec", "metrics", 0, "resource", "target", "averageUtilization") == 70

    pdb = _resource(resources, "PodDisruptionBudget", "lazycloud-scheduler")
    assert _value_path(pdb, "spec", "minAvailable") == 1
    assert (
        _string_path(pdb, "spec", "selector", "matchLabels", "app.kubernetes.io/component")
        == "scheduler"
    )

    container_worker = _resource(resources, "Deployment", "lazycloud-container-worker-default")
    assert _string_path(
        container_worker, "metadata", "annotations", "lazycloud.io/scaling-owner"
    ) == ("scheduler-worker-pool-controller")
    assert (
        _string_path(
            container_worker,
            "spec",
            "template",
            "metadata",
            "annotations",
            "lazycloud.io/scaling-owner",
        )
        == "scheduler-worker-pool-controller"
    )


def _yaml_documents_from_text(text: str, source: str) -> list[YamlMapping]:
    values = (
        None if value is None else _YAML_MAPPING_ADAPTER.validate_python(value)
        for value in yaml.safe_load_all(text)
    )
    return _yaml_documents_from_values(values, source)


def _default_container_worker_pool() -> YamlMapping:
    values = _mapping(
        _YAML_MAPPING_ADAPTER.validate_python(
            yaml.safe_load(
                (ROOT / "deploy/charts/lazycloud/values.yaml").read_text(encoding="utf-8")
            )
        ),
        "default Helm values",
    )
    pools = _mapping_sequence_path(values, "containerWorkers", "pools")
    assert len(pools) == 1
    return pools[0]


def _set_mapping_path(
    root: YamlMapping,
    path: tuple[str, ...],
    value: JsonValue,
) -> None:
    assert path
    if len(path) == 1:
        root[path[0]] = value
        return
    nested = _mapping(root[path[0]], f"worker pool override {'.'.join(path)}")
    _set_mapping_path(nested, path[1:], value)
    root[path[0]] = nested


def _shell_function(script: str, name: str) -> str:
    lines = script.splitlines()
    declaration = f"{name}() {{"
    start = next(index for index, line in enumerate(lines) if line.strip() == declaration)
    end = next(
        index for index, line in enumerate(lines[start + 1 :], start + 1) if line.strip() == "}"
    )
    return "\n".join(
        line.strip() if index == start else line
        for index, line in enumerate(lines[start : end + 1], start)
    )


def _run_shell_function(
    function: str,
    name: str,
    argument: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-ec", f'{function}\n{name} "$1"', name, argument],
        check=False,
        capture_output=True,
        text=True,
    )


def _yaml_documents_from_values(
    values: Iterable[YamlMapping | None], source: str
) -> list[YamlMapping]:
    documents: list[YamlMapping] = []
    for index, value in enumerate(values):
        if value is None:
            continue
        documents.append(_mapping(value, f"{source} document {index}"))
    return documents


def _names_by_kind(resources: list[YamlMapping]) -> set[tuple[str, str]]:
    return {
        (_string_path(item, "kind"), _string_path(item, "metadata", "name")) for item in resources
    }


def _resource(resources: list[YamlMapping], kind: str, name: str) -> YamlMapping:
    return next(
        item
        for item in resources
        if _string_path(item, "kind") == kind and _string_path(item, "metadata", "name") == name
    )


def _resource_by_component(
    resources: list[YamlMapping],
    kind: str,
    component: str,
) -> YamlMapping:
    matches = [
        item
        for item in resources
        if _string_path(item, "kind") == kind
        and _string_path(
            item,
            "metadata",
            "labels",
            "app.kubernetes.io/component",
        )
        == component
    ]
    assert len(matches) == 1, f"expected one {kind} with component {component}"
    return matches[0]


def _alert_rules(config_map: YamlMapping) -> dict[str, YamlMapping]:
    rules_yaml = _string_path(config_map, "data", "lazycloud-rules.yaml")
    payload = _mapping(
        _YAML_MAPPING_ADAPTER.validate_python(yaml.safe_load(rules_yaml)), "alert rules"
    )
    rules_by_alert: dict[str, YamlMapping] = {}
    for group in _mapping_sequence_path(payload, "groups"):
        for rule in _mapping_sequence_path(group, "rules"):
            alert = rule.get("alert")
            if isinstance(alert, str):
                rules_by_alert[alert] = rule
    return rules_by_alert


def _env_by_name(container: YamlMapping) -> dict[str, YamlMapping]:
    return {_string_path(item, "name"): item for item in _mapping_sequence_path(container, "env")}


def _string_env_by_name(container: YamlMapping) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in _mapping_sequence_path(container, "env"):
        if "value" in item:
            env[_string_path(item, "name")] = _string_path(item, "value")
    return env


def _mapping_sequence_path(root: JsonValue, *path: PathPart) -> list[YamlMapping]:
    sequence_path = _path_label(path)
    return [
        _mapping(item, f"{sequence_path}[{index}]")
        for index, item in enumerate(_sequence_path(root, *path))
    ]


def _string_mapping_path(root: JsonValue, *path: PathPart) -> dict[str, str]:
    mapping_path = _path_label(path)
    strings: dict[str, str] = {}
    for key, value in _mapping_path(root, *path).items():
        strings[key] = _string(value, f"{mapping_path}.{key}")
    return strings


def _mapping_path(root: JsonValue, *path: PathPart) -> YamlMapping:
    return _mapping(_value_path(root, *path), _path_label(path))


def _sequence_path(root: JsonValue, *path: PathPart) -> list[JsonValue]:
    return _sequence(_value_path(root, *path), _path_label(path))


def _string_path(root: JsonValue, *path: PathPart) -> str:
    return _string(_value_path(root, *path), _path_label(path))


def _value_path(root: JsonValue, *path: PathPart) -> JsonValue:
    current = root
    current_path: tuple[PathPart, ...] = ()
    for part in path:
        context = _path_label(current_path)
        if isinstance(part, str):
            mapping = _mapping(current, context)
            assert part in mapping, f"{context} is missing key {part!r}"
            current = mapping[part]
        else:
            sequence = _sequence(current, context)
            assert -len(sequence) <= part < len(sequence), f"{context} is missing index {part}"
            current = sequence[part]
        current_path = (*current_path, part)
    return current


def _mapping(value: JsonValue, context: str) -> YamlMapping:
    assert isinstance(value, dict), f"{context} must be a mapping"
    mapping: YamlMapping = {}
    for key, item in value.items():
        assert isinstance(key, str), f"{context} contains non-string key {key!r}"
        mapping[key] = item
    return mapping


def _sequence(value: JsonValue, context: str) -> list[JsonValue]:
    assert isinstance(value, list), f"{context} must be a sequence"
    sequence: list[JsonValue] = []
    for item in value:
        sequence.append(item)
    return sequence


def _string(value: JsonValue, context: str) -> str:
    assert isinstance(value, str), f"{context} must be a string"
    return value


def _path_label(path: tuple[PathPart, ...]) -> str:
    label = "$"
    for part in path:
        label = f"{label}.{part}" if isinstance(part, str) else f"{label}[{part}]"
    return label


def _backend_secret_env_names() -> set[str]:
    return {
        "LAZYCLOUD_DATABASE_URL",
        "LAZYCLOUD_REDIS_URL",
        "LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL",
        "LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID",
        "LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY",
    }

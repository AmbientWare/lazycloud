"""Copy frozen platform storage and relocate its durable coordinates without deleting sources."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Annotated, Literal
from urllib.request import Request, urlopen

import boto3
from database.client import DatabaseClient
from database.repositories.object_storage_migration import ObjectStorageMigrationRepository
from database.settings import DatabaseApplicationName, DatabaseSettings
from deploy.chart_values import Infrastructure, ObjectStoreInfrastructure
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from shared.contracts import ContractModel
from storage.object_storage_migration import ObjectStorageMigration
from storage_client.s3 import S3ObjectStoreClient, S3ObjectStoreSettings

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]+$")]


class MigrationInput(ContractModel):
    source: ObjectStoreInfrastructure
    source_profile: str
    cloudflare_account_id: Identifier
    token_owner: Literal["account", "user"]
    writer_token_id: Identifier
    kubernetes_context: str
    namespace: Identifier
    argocd_namespace: Identifier
    argocd_application: Identifier


class _Credentials(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    AccessKeyId: str = Field(repr=False)
    SecretAccessKey: str = Field(repr=False)
    SessionToken: str = Field(default="", repr=False)


class _Permission(BaseModel):
    name: str


class _Policy(BaseModel):
    effect: str
    permission_groups: list[_Permission]


class _Token(BaseModel):
    id: str = Field(repr=False)
    status: Literal["active", "disabled", "expired"]
    policies: list[_Policy]


class _TokenResponse(BaseModel):
    success: bool
    result: _Token


class _KubernetesObject(BaseModel):
    kind: str
    metadata: dict[str, JsonValue]
    spec: dict[str, JsonValue] = Field(default_factory=dict)
    status: dict[str, JsonValue] = Field(default_factory=dict)
    operation: JsonValue = None


class _KubernetesList(BaseModel):
    items: list[_KubernetesObject]


def _run(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode:
        # CLI errors can contain credentials or database endpoints supplied by the operator.
        raise RuntimeError(f"{arguments[0]} {arguments[1]} failed with exit {result.returncode}")
    return result.stdout


def _token(configuration: MigrationInput, token_id: str) -> _Token:
    scope = (
        f"accounts/{configuration.cloudflare_account_id}"
        if configuration.token_owner == "account"
        else "user"
    )
    request = Request(
        f"https://api.cloudflare.com/client/v4/{scope}/tokens/{token_id}",
        headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"},
    )
    with urlopen(request, timeout=20) as response:
        result = _TokenResponse.model_validate_json(response.read())
    if not result.success or result.result.id != token_id:
        raise RuntimeError("Cloudflare did not confirm the requested token identity")
    return result.result


def assert_writers_stopped(configuration: MigrationInput, source_access_key: str) -> None:
    kubectl = ["kubectl", "--context", configuration.kubernetes_context]
    application = _KubernetesObject.model_validate_json(
        _run(
            [
                *kubectl,
                "-n",
                configuration.argocd_namespace,
                "get",
                "application",
                configuration.argocd_application,
                "-o",
                "json",
            ]
        )
    )
    destination = application.spec.get("destination")
    if not isinstance(destination, dict) or destination.get("namespace") != configuration.namespace:
        raise RuntimeError("Argo application does not own the migration namespace")
    policy = application.spec.get("syncPolicy")
    if isinstance(policy, dict):
        automated = policy.get("automated")
        if isinstance(automated, dict) and automated.get("enabled") is not False:
            raise RuntimeError("disable Argo automatic sync before storage migration")
    operation = application.status.get("operationState")
    if application.operation is not None or (
        isinstance(operation, dict) and operation.get("phase") in {"Running", "Terminating"}
    ):
        raise RuntimeError("finish the active Argo operation before storage migration")
    resources = _KubernetesList.model_validate_json(
        _run(
            [
                *kubectl,
                "-n",
                configuration.namespace,
                "get",
                "deployments,statefulsets,daemonsets,pods,jobs,cronjobs",
                "-o",
                "json",
            ]
        )
    )
    if not any(resource.kind == "Deployment" for resource in resources.items):
        raise RuntimeError("migration namespace has no platform deployments")
    for resource in resources.items:
        stopped = True
        if resource.kind in {"Deployment", "StatefulSet"}:
            stopped = resource.spec.get("replicas", 1) == 0
        elif resource.kind == "DaemonSet":
            stopped = resource.status.get("desiredNumberScheduled", 0) == 0
        elif resource.kind == "Pod":
            stopped = resource.status.get("phase") in {"Succeeded", "Failed"}
        elif resource.kind == "Job":
            stopped = resource.status.get("active", 0) == 0
        elif resource.kind == "CronJob":
            stopped = resource.spec.get("suspend") is True
        if not stopped:
            raise RuntimeError(
                f"storage writer is not stopped: {resource.kind}/{resource.metadata.get('name')}"
            )
    if source_access_key == configuration.writer_token_id:
        raise RuntimeError("migration reads require a separate read-only source token")
    if _token(configuration, configuration.writer_token_id).status == "active":
        raise RuntimeError("disable the deployed platform storage parent token before migration")
    reader = _token(configuration, source_access_key)
    read_permissions = {"Workers R2 Storage Read", "Workers R2 Storage Bucket Item Read"}
    if (
        reader.status != "active"
        or not reader.policies
        or any(
            policy.effect != "allow"
            or not policy.permission_groups
            or any(
                permission.name not in read_permissions for permission in policy.permission_groups
            )
            for policy in reader.policies
        )
    ):
        raise RuntimeError("migration source token must grant only R2 reads")


class _Arguments(argparse.Namespace):
    configuration: Path
    infrastructure: Path
    execute: bool


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--infrastructure", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    arguments = _Arguments()
    parser.parse_args(namespace=arguments)
    configuration = MigrationInput.model_validate_json(arguments.configuration.read_bytes())
    infrastructure = Infrastructure.model_validate_json(arguments.infrastructure.read_bytes())
    if configuration.namespace != infrastructure.deployment:
        raise RuntimeError("migration namespace must match the target infrastructure deployment")
    if configuration.source.endpoint_url != (
        f"https://{configuration.cloudflare_account_id}.r2.cloudflarestorage.com"
    ):
        raise RuntimeError("source must name the configured Cloudflare account's R2 endpoint")
    account = _run(
        [
            "aws",
            "--profile",
            "default",
            "sts",
            "get-caller-identity",
            "--query",
            "Account",
            "--output",
            "text",
        ]
    ).strip()
    if account != infrastructure.fleet.account_id:
        raise RuntimeError("AWS default profile does not own the destination deployment")
    boto3.setup_default_session(profile_name="default", region_name=infrastructure.region)
    source_keys = _Credentials.model_validate_json(
        _run(
            [
                "aws",
                "configure",
                "export-credentials",
                "--profile",
                configuration.source_profile,
                "--format",
                "process",
            ]
        )
    )
    if source_keys.SessionToken:
        raise RuntimeError("migration requires an independent permanent read-only source token")
    source = S3ObjectStoreClient.from_settings(
        S3ObjectStoreSettings(
            **configuration.source.model_dump(),
            access_key_id=source_keys.AccessKeyId,
            secret_access_key=source_keys.SecretAccessKey,
            session_token="",
        )
    )
    target = S3ObjectStoreClient.from_settings(
        S3ObjectStoreSettings(
            **infrastructure.object_store.model_dump(),
            access_key_id="",
            secret_access_key="",
            session_token="",
        )
    )
    database = DatabaseClient.from_settings(
        DatabaseSettings(application_name=DatabaseApplicationName.Admin)
    )
    try:
        with database.session() as session:
            repository = ObjectStorageMigrationRepository(session)
            repository.lock_writers()
            migration = ObjectStorageMigration(source, target)
            plan = migration.plan(repository.snapshot())
            print(
                f"Migration covers {len(plan.buckets)} buckets, {len(plan.workspaces)} workspaces, "
                f"{len(plan.objects)} objects, {len(plan.archives)} image archives"
            )
            for source_bucket, target_bucket in plan.buckets.items():
                print(f"{source_bucket} -> {target_bucket}")
            if not arguments.execute:
                print(
                    "Plan only. Execute requires stopped writers and disabled source write access."
                )
                return

            def guard() -> None:
                assert_writers_stopped(configuration, source_keys.AccessKeyId)

            guard()
            for bucket in plan.buckets.values():
                # Terraform owns the application bucket; only managed workspace buckets
                # are created here, after the production issuer's naming validation.
                if bucket != infrastructure.object_store.bucket:
                    target.create_bucket(bucket)
                _run(
                    [
                        "aws",
                        "--profile",
                        "default",
                        "s3api",
                        "head-bucket",
                        "--bucket",
                        bucket,
                        "--expected-bucket-owner",
                        infrastructure.fleet.account_id,
                        "--region",
                        infrastructure.region,
                    ]
                )
                if bucket != infrastructure.object_store.bucket:
                    target.configure_workspace_bucket(
                        bucket, public_origin=infrastructure.public_origin
                    )
            migration.copy_and_relocate(
                plan, repository, assert_writers_stopped=guard, progress=print
            )
        print("Storage copy verified and database coordinates committed. Source buckets retained.")
    finally:
        source.close()
        target.close()
        database.dispose()


if __name__ == "__main__":
    main()

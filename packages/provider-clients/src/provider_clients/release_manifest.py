"""The published contract of an immutable connected-AWS release.

The release publisher writes this document and the control plane reads it, so it
is one schema with two ends rather than a publisher format the runtime restates.
Every value a deployment would otherwise copy out of a release is derived here
from the release itself, which is what makes disagreement impossible instead of
merely unlikely.
"""

from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import urlparse

from provider_aws import AwsAccountConnectionTemplatePublication
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = 1
AMI_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
S3_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
WORKER_IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
AGENT_BINARY_DIRECTORY = "agent-binarys"
AGENT_AMD64_FILENAME = "lazycloud-agent-linux-amd64"


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReleaseObject(ReleaseModel):
    local_path: str
    object_key: str
    public_url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    content_type: str
    cache_control: str = "public, max-age=31536000, immutable"


class AwsReleaseManifest(ReleaseModel):
    schema_version: int
    release_version: str
    bucket: str
    region: str
    manifest_object_key: str
    manifest_public_url: str
    connection_template_version: str
    connection_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_artifact_version: str
    agent_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_worker_image: str = Field(pattern=WORKER_IMAGE_PATTERN.pattern)
    capacity_cpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    capacity_gpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    objects: list[ReleaseObject]
    deployment_environment: dict[str, str]

    @property
    def connection_template_object(self) -> ReleaseObject:
        return self._sole_object(self._connection_template_objects())

    @property
    def agent_artifact_object(self) -> ReleaseObject:
        return self._sole_object(self._agent_artifact_objects())

    @property
    def agent_artifact_sha256_by_arch(self) -> dict[str, str]:
        return {"amd64": self.agent_artifact_sha256}

    def _connection_template_objects(self) -> list[ReleaseObject]:
        suffix = f"/connection-templates/{self.connection_template_sha256}/template.json"
        return [
            release_object
            for release_object in self.objects
            if release_object.object_key.endswith(suffix)
        ]

    def _agent_artifact_objects(self) -> list[ReleaseObject]:
        suffix = (
            f"/agents/{self.agent_artifact_version}/{self.agent_artifact_sha256}/"
            f"{AGENT_AMD64_FILENAME}"
        )
        return [
            release_object
            for release_object in self.objects
            if release_object.object_key.endswith(suffix)
        ]

    @staticmethod
    def _sole_object(candidates: list[ReleaseObject]) -> ReleaseObject:
        if len(candidates) != 1:
            raise ValueError("AWS release does not contain exactly one of a required object")
        return candidates[0]

    @model_validator(mode="after")
    def validate_release(self) -> AwsReleaseManifest:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported AWS release manifest schema: {self.schema_version}")
        if not VERSION_PATTERN.fullmatch(self.release_version):
            raise ValueError("invalid AWS release version")
        for catalog_name, catalog in (
            ("CPU", self.capacity_cpu_ami_ids),
            ("GPU", self.capacity_gpu_ami_ids),
        ):
            for ami_region, ami_id in catalog.items():
                if not REGION_PATTERN.fullmatch(ami_region):
                    msg = f"AWS release {catalog_name} AMI catalog has an invalid region"
                    raise ValueError(msg)
                if not AMI_PATTERN.fullmatch(ami_id):
                    msg = f"AWS release {catalog_name} AMI catalog has an invalid AMI ID"
                    raise ValueError(msg)
        if self.agent_artifact_version != self.release_version:
            raise ValueError("agent artifact version must equal the AWS release version")
        if not S3_NAME_PATTERN.fullmatch(self.bucket):
            raise ValueError("invalid AWS release bucket name")
        if not REGION_PATTERN.fullmatch(self.region):
            raise ValueError("invalid AWS release region")
        object_keys = [release_object.object_key for release_object in self.objects]
        if len(object_keys) != len(set(object_keys)):
            raise ValueError("AWS release object keys must be unique")
        if len(self.objects) != 2:
            raise ValueError("AWS release must contain one template and one agent object")
        for release_object in self.objects:
            expected_url = s3_public_url(self.bucket, self.region, release_object.object_key)
            if release_object.public_url != expected_url:
                raise ValueError("AWS release object URL does not match its S3 object key")
        if self.manifest_public_url != s3_public_url(
            self.bucket,
            self.region,
            self.manifest_object_key,
        ):
            raise ValueError("AWS release manifest URL does not match its S3 object key")
        template_objects = self._connection_template_objects()
        agent_objects = self._agent_artifact_objects()
        if len(template_objects) != 1 or template_objects[0].sha256 != (
            self.connection_template_sha256
        ):
            raise ValueError("AWS release template object does not match the bundled template")
        if len(agent_objects) != 1 or agent_objects[0].sha256 != self.agent_artifact_sha256:
            raise ValueError("AWS release agent object does not match the staged agent")
        template_url = self.deployment_environment.get("LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL", "")
        AwsAccountConnectionTemplatePublication(
            url=template_url,
            sha256=self.connection_template_sha256,
        )
        if self.connection_template_sha256 not in urlparse(template_url).path:
            raise ValueError("connection template URL must contain its bundled digest")
        if (
            self.deployment_environment.get("LAZYCLOUD_AGENT_BINARY_VERSION")
            != self.agent_artifact_version
        ):
            raise ValueError("deployment environment agent version does not match the release")
        if (
            self.deployment_environment.get("LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST")
            != self.container_worker_image
        ):
            raise ValueError("deployment environment worker image does not match the release")
        expected_environment = {
            "LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH": json.dumps(
                {"amd64": self.agent_artifact_sha256},
                sort_keys=True,
                separators=(",", ":"),
            ),
            "LAZYCLOUD_AGENT_BINARY_VERSION": self.agent_artifact_version,
            "LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL": agent_objects[0].public_url,
            "LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST": self.container_worker_image,
            "LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL": template_objects[0].public_url,
        }
        for env_name, catalog in (
            ("LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS", self.capacity_cpu_ami_ids),
            ("LAZYCLOUD_AWS_CAPACITY_GPU_AMI_IDS", self.capacity_gpu_ami_ids),
        ):
            if catalog:
                expected_environment[env_name] = json.dumps(
                    catalog,
                    sort_keys=True,
                    separators=(",", ":"),
                )
        if self.deployment_environment != expected_environment:
            raise ValueError("AWS release deployment environment is incomplete or inconsistent")
        return self


def s3_public_url(bucket: str, region: str, object_key: str) -> str:
    encoded_key = "/".join(urllib.request.pathname2url(part) for part in object_key.split("/"))
    return f"https://s3.{region}.amazonaws.com/{bucket}/{encoded_key}"


__all__ = [
    "AGENT_AMD64_FILENAME",
    "AGENT_BINARY_DIRECTORY",
    "AMI_PATTERN",
    "REGION_PATTERN",
    "S3_NAME_PATTERN",
    "SCHEMA_VERSION",
    "VERSION_PATTERN",
    "WORKER_IMAGE_PATTERN",
    "AwsReleaseManifest",
    "ReleaseModel",
    "ReleaseObject",
    "s3_public_url",
]

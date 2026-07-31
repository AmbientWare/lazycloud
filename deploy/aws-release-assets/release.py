"""Immutable connected-AWS release publication."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from provider_aws import (
    AwsAccountConnectionTemplatePublication,
    aws_account_connection_template_bytes,
    aws_account_connection_template_identity,
    validate_aws_account_connection_template_policy,
)
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, model_validator

SCHEMA_VERSION = 1
_AMI_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_S3_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_WORKER_IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_AGENT_BINARY_DIRECTORY = "agent-binarys"
_AGENT_AMD64_FILENAME = "lazycloud-agent-linux-amd64"
_OCI_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
_DOCKER_IMAGE_MANIFEST_MEDIA_TYPE = "application/vnd.docker.distribution.manifest.v2+json"
_DOCKER_ATTESTATION_REFERENCE_TYPE = "attestation-manifest"
_DOCKER_REFERENCE_DIGEST_ANNOTATION = "vnd.docker.reference.digest"
_DOCKER_REFERENCE_TYPE_ANNOTATION = "vnd.docker.reference.type"


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentArtifactEntry(ReleaseModel):
    os: str
    arch: str
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class AgentArtifactManifest(ReleaseModel):
    schema_version: int
    version: str
    artifacts: list[AgentArtifactEntry]


class ReleaseObject(ReleaseModel):
    local_path: str
    object_key: str
    public_url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    content_type: str
    cache_control: str = "public, max-age=31536000, immutable"


class AwsHeadObjectResponse(ReleaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    content_length: int = Field(alias="ContentLength")
    checksum_sha256: str = Field(alias="ChecksumSHA256")


class DockerImagePlatform(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    architecture: str
    os: str


class DockerImageDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    media_type: str = Field(alias="mediaType")
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    platform: DockerImagePlatform
    annotations: dict[str, str] = Field(default_factory=dict)


class DockerManifestInspection(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    reference: str = Field(alias="Ref")
    descriptor: DockerImageDescriptor = Field(alias="Descriptor")


class DockerManifestInspectionPayload(
    RootModel[DockerManifestInspection | list[DockerManifestInspection]]
):
    pass


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
    container_worker_image: str = Field(pattern=_WORKER_IMAGE_PATTERN.pattern)
    capacity_cpu_ami_ids: dict[str, str] = Field(default_factory=dict)
    objects: list[ReleaseObject]
    deployment_environment: dict[str, str]

    @model_validator(mode="after")
    def validate_release(self) -> AwsReleaseManifest:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported AWS release manifest schema: {self.schema_version}")
        if not _VERSION_PATTERN.fullmatch(self.release_version):
            raise ValueError("invalid AWS release version")
        for ami_region, ami_id in self.capacity_cpu_ami_ids.items():
            if not _REGION_PATTERN.fullmatch(ami_region):
                raise ValueError("AWS release CPU AMI catalog has an invalid region")
            if not _AMI_PATTERN.fullmatch(ami_id):
                raise ValueError("AWS release CPU AMI catalog has an invalid AMI ID")
        if self.agent_artifact_version != self.release_version:
            raise ValueError("agent artifact version must equal the AWS release version")
        if not _S3_NAME_PATTERN.fullmatch(self.bucket):
            raise ValueError("invalid AWS release bucket name")
        if not _REGION_PATTERN.fullmatch(self.region):
            raise ValueError("invalid AWS release region")
        object_keys = [release_object.object_key for release_object in self.objects]
        if len(object_keys) != len(set(object_keys)):
            raise ValueError("AWS release object keys must be unique")
        if len(self.objects) != 2:
            raise ValueError("AWS release must contain one template and one agent object")
        for release_object in self.objects:
            expected_url = _s3_public_url(self.bucket, self.region, release_object.object_key)
            if release_object.public_url != expected_url:
                raise ValueError("AWS release object URL does not match its S3 object key")
        if self.manifest_public_url != _s3_public_url(
            self.bucket,
            self.region,
            self.manifest_object_key,
        ):
            raise ValueError("AWS release manifest URL does not match its S3 object key")
        template_objects = [
            release_object
            for release_object in self.objects
            if release_object.object_key.endswith(
                f"/connection-templates/{self.connection_template_sha256}/template.json"
            )
        ]
        agent_objects = [
            release_object
            for release_object in self.objects
            if release_object.object_key.endswith(
                f"/agents/{self.agent_artifact_version}/{self.agent_artifact_sha256}/"
                f"{_AGENT_AMD64_FILENAME}"
            )
        ]
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
        if self.capacity_cpu_ami_ids:
            expected_environment["LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS"] = json.dumps(
                self.capacity_cpu_ami_ids,
                sort_keys=True,
                separators=(",", ":"),
            )
        if self.deployment_environment != expected_environment:
            raise ValueError("AWS release deployment environment is incomplete or inconsistent")
        return self


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage, publish, and verify immutable connected-AWS release assets."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage = subparsers.add_parser("stage")
    stage.add_argument("--version", required=True)
    stage.add_argument("--agent-version-dir", type=Path, required=True)
    stage.add_argument("--worker-image", required=True)
    stage.add_argument("--bucket", required=True)
    stage.add_argument("--region", required=True)
    stage.add_argument("--key-prefix", default="connected-aws")
    stage.add_argument(
        "--cpu-ami-ids",
        default="{}",
        help="JSON object mapping AWS region to the baked CPU node AMI ID",
    )
    stage.add_argument("--output", type=Path, required=True)

    validate_local = subparsers.add_parser("validate-local")
    validate_local.add_argument("--manifest", type=Path, required=True)

    publish = subparsers.add_parser("publish")
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--aws-cli", default="aws")

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--docker-cli", default="docker")
    verify.add_argument("--aws-cli", default="aws")
    verify.add_argument(
        "--aws-cli-verify",
        action="store_true",
        help="also verify baked capacity AMIs with AWS credentials (they are private)",
    )

    args = parser.parse_args()
    if args.command == "stage":
        manifest = stage_release(
            version=args.version,
            agent_version_dir=args.agent_version_dir,
            worker_image=args.worker_image,
            bucket=args.bucket,
            region=args.region,
            key_prefix=args.key_prefix,
            cpu_ami_ids=_parse_cpu_ami_ids(args.cpu_ami_ids),
            output=args.output,
        )
        print(manifest.model_dump_json())
        return
    manifest_path = args.manifest.resolve()
    manifest = load_release_manifest(manifest_path)
    if args.command == "validate-local":
        validate_local_release(manifest_path, manifest)
        print(manifest.model_dump_json())
        return
    if args.command == "publish":
        publish_release(
            manifest,
            bundle_root=manifest_path.parent,
            aws_cli=args.aws_cli,
        )
        print(manifest.model_dump_json())
        return
    verify_release(
        manifest,
        docker_cli=args.docker_cli,
        aws_cli=args.aws_cli,
        verify_amis=args.aws_cli_verify,
    )
    print(manifest.model_dump_json())


def _parse_cpu_ami_ids(raw: str) -> dict[str, str]:
    try:
        parsed = RootModel[dict[str, str]].model_validate_json(raw).root
    except ValidationError as exc:
        raise ValueError("--cpu-ami-ids must be a JSON object mapping region to AMI ID") from exc
    return parsed


def stage_release(
    *,
    version: str,
    agent_version_dir: Path,
    worker_image: str,
    bucket: str,
    region: str,
    key_prefix: str,
    output: Path,
    cpu_ami_ids: dict[str, str] | None = None,
) -> AwsReleaseManifest:
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("release version contains invalid characters")
    if not _S3_NAME_PATTERN.fullmatch(bucket):
        raise ValueError("invalid AWS release bucket name")
    normalized_region = region.strip().lower()
    if not _REGION_PATTERN.fullmatch(normalized_region):
        raise ValueError("invalid AWS release region")
    normalized_prefix = key_prefix.strip().strip("/")
    if not normalized_prefix or any(part in {".", ".."} for part in normalized_prefix.split("/")):
        raise ValueError("AWS release key prefix is invalid")
    if not _WORKER_IMAGE_PATTERN.fullmatch(worker_image):
        raise ValueError("container-worker image must use an immutable sha256 digest reference")
    normalized_cpu_ami_ids: dict[str, str] = {}
    for raw_ami_region, raw_ami_id in sorted((cpu_ami_ids or {}).items()):
        ami_region = raw_ami_region.strip().lower()
        ami_id = raw_ami_id.strip().lower()
        if not _REGION_PATTERN.fullmatch(ami_region):
            raise ValueError("CPU AMI catalog has an invalid region")
        if not _AMI_PATTERN.fullmatch(ami_id):
            raise ValueError("CPU AMI catalog has an invalid AMI ID")
        normalized_cpu_ami_ids[ami_region] = ami_id

    agent_manifest_path = agent_version_dir.resolve() / "manifest.json"
    agent_manifest = AgentArtifactManifest.model_validate_json(
        agent_manifest_path.read_text(encoding="utf-8")
    )
    if agent_manifest.schema_version != 1 or agent_manifest.version != version:
        raise ValueError("agent artifact manifest does not match the release version")
    amd64_artifacts = [
        artifact
        for artifact in agent_manifest.artifacts
        if artifact.os == "linux" and artifact.arch == "amd64"
    ]
    if len(amd64_artifacts) != 1:
        raise ValueError("agent artifact manifest must contain exactly one linux/amd64 artifact")
    agent = amd64_artifacts[0]
    if agent.filename != _AGENT_AMD64_FILENAME:
        raise ValueError("linux/amd64 agent artifact filename is invalid")
    agent_source = agent_version_dir.resolve() / agent.filename
    _verify_file(agent_source, expected_sha256=agent.sha256, expected_size=agent.size_bytes)
    with agent_source.open("rb") as executable:
        if executable.read(4) != b"\x7fELF":
            raise ValueError("linux/amd64 agent artifact is not an ELF executable")

    template = aws_account_connection_template_bytes()
    template_identity = aws_account_connection_template_identity()
    if _sha256_bytes(template) != template_identity.sha256:
        raise RuntimeError("bundled AWS connection template identity is inconsistent")

    release_root = output.resolve() / version
    template_local = Path("objects") / "connection-template.json"
    agent_local = Path(_AGENT_BINARY_DIRECTORY) / version / agent.filename
    template_path = release_root / template_local
    agent_path = release_root / agent_local
    _write_immutable(template_path, template)
    _copy_immutable(agent_source, agent_path)
    agent_path.chmod(0o755)

    template_key = (
        f"{normalized_prefix}/connection-templates/{template_identity.sha256}/template.json"
    )
    agent_key = f"{normalized_prefix}/agents/{version}/{agent.sha256}/{agent.filename}"
    manifest_key = f"{normalized_prefix}/releases/{version}/manifest.json"
    template_url = _s3_public_url(bucket, normalized_region, template_key)
    agent_url = _s3_public_url(bucket, normalized_region, agent_key)
    manifest_url = _s3_public_url(bucket, normalized_region, manifest_key)
    AwsAccountConnectionTemplatePublication(
        url=template_url,
        sha256=template_identity.sha256,
    )

    manifest = AwsReleaseManifest(
        schema_version=SCHEMA_VERSION,
        release_version=version,
        bucket=bucket,
        region=normalized_region,
        manifest_object_key=manifest_key,
        manifest_public_url=manifest_url,
        connection_template_version=template_identity.version,
        connection_template_sha256=template_identity.sha256,
        agent_artifact_version=version,
        agent_artifact_sha256=agent.sha256,
        container_worker_image=worker_image,
        capacity_cpu_ami_ids=normalized_cpu_ami_ids,
        objects=[
            ReleaseObject(
                local_path=template_local.as_posix(),
                object_key=template_key,
                public_url=template_url,
                sha256=template_identity.sha256,
                size_bytes=len(template),
                content_type="application/json",
            ),
            ReleaseObject(
                local_path=agent_local.as_posix(),
                object_key=agent_key,
                public_url=agent_url,
                sha256=agent.sha256,
                size_bytes=agent.size_bytes,
                content_type="application/octet-stream",
            ),
        ],
        deployment_environment={
            "LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH": json.dumps(
                {"amd64": agent.sha256}, sort_keys=True, separators=(",", ":")
            ),
            "LAZYCLOUD_AGENT_BINARY_VERSION": version,
            "LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL": agent_url,
            "LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST": worker_image,
            "LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL": template_url,
            **(
                {
                    "LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS": json.dumps(
                        normalized_cpu_ami_ids, sort_keys=True, separators=(",", ":")
                    )
                }
                if normalized_cpu_ami_ids
                else {}
            ),
        },
    )
    manifest_payload = _manifest_payload(manifest)
    _write_immutable(release_root / "manifest.json", manifest_payload)
    return manifest


def load_release_manifest(path: Path) -> AwsReleaseManifest:
    try:
        manifest = AwsReleaseManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise ValueError(f"invalid AWS release manifest: {path}") from exc
    identity = aws_account_connection_template_identity()
    if (
        manifest.connection_template_version != identity.version
        or manifest.connection_template_sha256 != identity.sha256
    ):
        raise ValueError("AWS release manifest does not contain the current connection template")
    return manifest


def validate_local_release(
    manifest_path: Path,
    manifest: AwsReleaseManifest,
) -> Path:
    bundle_root = manifest_path.expanduser().resolve().parent
    if manifest_path.read_bytes() != _manifest_payload(manifest):
        raise RuntimeError("local AWS release manifest bytes are not canonical")

    template_objects = [
        release_object
        for release_object in manifest.objects
        if release_object.object_key.endswith(
            f"/connection-templates/{manifest.connection_template_sha256}/template.json"
        )
    ]
    if len(template_objects) != 1:
        raise RuntimeError("local AWS release bundle does not contain its connection template")
    for release_object in manifest.objects:
        path = _release_object_path(bundle_root, release_object)
        _verify_file(
            path,
            expected_sha256=release_object.sha256,
            expected_size=release_object.size_bytes,
        )
    validate_aws_account_connection_template_policy(
        _release_object_path(bundle_root, template_objects[0]).read_bytes()
    )
    return validated_local_agent_artifact_root(manifest_path, manifest)


def validated_local_agent_artifact_root(
    manifest_path: Path,
    manifest: AwsReleaseManifest,
) -> Path:
    bundle_root = manifest_path.expanduser().resolve().parent
    expected_local_path = (
        Path(_AGENT_BINARY_DIRECTORY) / manifest.agent_artifact_version / _AGENT_AMD64_FILENAME
    )
    agent_objects = [
        release_object
        for release_object in manifest.objects
        if release_object.object_key.endswith(
            f"/agents/{manifest.agent_artifact_version}/{manifest.agent_artifact_sha256}/"
            f"{_AGENT_AMD64_FILENAME}"
        )
    ]
    if len(agent_objects) != 1 or Path(agent_objects[0].local_path) != expected_local_path:
        raise ValueError(
            "AWS release bundle does not retain the agent in its versioned artifact directory"
        )

    artifact_root = bundle_root / _AGENT_BINARY_DIRECTORY
    version_root = artifact_root / manifest.agent_artifact_version
    agent_path = bundle_root / expected_local_path
    resolved_artifact_root = artifact_root.resolve()
    resolved_agent_path = agent_path.resolve()
    if (
        not resolved_artifact_root.is_relative_to(bundle_root)
        or not resolved_agent_path.is_relative_to(resolved_artifact_root)
        or artifact_root.is_symlink()
        or version_root.is_symlink()
        or agent_path.is_symlink()
    ):
        raise ValueError("AWS release agent artifact path is not a retained bundle path")
    _verify_file(
        agent_path,
        expected_sha256=manifest.agent_artifact_sha256,
        expected_size=agent_objects[0].size_bytes,
    )
    if stat.S_IMODE(agent_path.stat().st_mode) != 0o755:
        raise ValueError("AWS release agent artifact mode must be 0755")
    with agent_path.open("rb") as executable:
        if executable.read(4) != b"\x7fELF":
            raise ValueError("AWS release agent artifact is not an ELF executable")
    return resolved_artifact_root


def publish_release(
    manifest: AwsReleaseManifest,
    *,
    bundle_root: Path,
    aws_cli: str,
) -> None:
    root = bundle_root.resolve()
    for release_object in manifest.objects:
        source = _release_object_path(root, release_object)
        _publish_object(
            source,
            release_object,
            bucket=manifest.bucket,
            region=manifest.region,
            aws_cli=aws_cli,
        )
    manifest_path = root / "manifest.json"
    expected_manifest = _manifest_payload(manifest)
    if manifest_path.read_bytes() != expected_manifest:
        raise RuntimeError("release manifest bytes do not match the validated manifest")
    manifest_object = ReleaseObject(
        local_path="manifest.json",
        object_key=manifest.manifest_object_key,
        public_url=manifest.manifest_public_url,
        sha256=_sha256_bytes(expected_manifest),
        size_bytes=len(expected_manifest),
        content_type="application/json",
    )
    _publish_object(
        manifest_path,
        manifest_object,
        bucket=manifest.bucket,
        region=manifest.region,
        aws_cli=aws_cli,
    )


def verify_release(
    manifest: AwsReleaseManifest,
    *,
    docker_cli: str = "docker",
    aws_cli: str = "aws",
    verify_amis: bool = False,
) -> None:
    for release_object in manifest.objects:
        payload = _verify_public_object(release_object)
        if release_object.object_key.endswith(
            f"/connection-templates/{manifest.connection_template_sha256}/template.json"
        ):
            validate_aws_account_connection_template_policy(payload)
    manifest_payload = _download_public(manifest.manifest_public_url)
    published_manifest = AwsReleaseManifest.model_validate_json(manifest_payload)
    if published_manifest != manifest:
        raise RuntimeError("published AWS release manifest does not match the local manifest")
    with tempfile.TemporaryDirectory(prefix="lazycloud-anonymous-docker-") as docker_config:
        environment = {**os.environ, "DOCKER_CONFIG": docker_config}
        result = subprocess.run(
            [
                docker_cli,
                "manifest",
                "inspect",
                "--verbose",
                manifest.container_worker_image,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    if result.returncode != 0:
        raise RuntimeError(
            "container-worker image is not anonymously readable by immutable digest: "
            f"{_command_error(result)}"
        )
    _validate_worker_image_platform(
        result.stdout,
        expected_reference=manifest.container_worker_image,
    )
    if manifest.capacity_cpu_ami_ids and not verify_amis:
        print(
            "note: skipping capacity AMI verification; baked AMIs are private and require "
            "AWS credentials (rerun verify with --aws-cli-verify)",
            file=sys.stderr,
        )
    elif verify_amis:
        _verify_capacity_amis(manifest.capacity_cpu_ami_ids, aws_cli=aws_cli)


class _DescribedImage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    image_id: str = Field(alias="ImageId")
    state: str = Field(alias="State")


class _DescribeImagesResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    images: list[_DescribedImage] = Field(default_factory=list, alias="Images")


def _verify_capacity_amis(cpu_ami_ids: dict[str, str], *, aws_cli: str) -> None:
    for region, ami_id in sorted(cpu_ami_ids.items()):
        result = subprocess.run(
            [
                aws_cli,
                "ec2",
                "describe-images",
                "--image-ids",
                ami_id,
                "--region",
                region,
                "--output",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "AWS_PAGER": ""},
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"capacity AMI {ami_id} is not describable in {region}: {_command_error(result)}"
            )
        described = _DescribeImagesResponse.model_validate_json(result.stdout).images
        if len(described) != 1 or described[0].image_id != ami_id:
            raise RuntimeError(f"AWS did not return the exact capacity AMI {ami_id} in {region}")
        if described[0].state != "available":
            raise RuntimeError(
                f"capacity AMI {ami_id} in {region} is not available ({described[0].state})"
            )


def _validate_worker_image_platform(payload: str, *, expected_reference: str) -> None:
    if not _WORKER_IMAGE_PATTERN.fullmatch(expected_reference):
        raise RuntimeError("container-worker reference must use an immutable sha256 digest")
    try:
        parsed = DockerManifestInspectionPayload.model_validate_json(payload).root
    except ValidationError as exc:
        raise RuntimeError(
            "container-worker digest must resolve to an inspectable image manifest"
        ) from exc
    inspections = parsed if isinstance(parsed, list) else [parsed]
    if not inspections:
        raise RuntimeError("container-worker digest resolved to an empty image index")

    repository, expected_digest = expected_reference.rsplit("@", maxsplit=1)
    if not isinstance(parsed, list):
        inspection = inspections[0]
        if (
            inspection.reference != expected_reference
            or inspection.descriptor.digest != expected_digest
            or not _is_linux_amd64_image(inspection.descriptor)
        ):
            raise RuntimeError("container-worker digest is not the exact Linux amd64 release image")
        return

    descriptor_digests = [inspection.descriptor.digest for inspection in inspections]
    if len(descriptor_digests) != len(set(descriptor_digests)):
        raise RuntimeError("container-worker image index contains duplicate descriptors")
    for inspection in inspections:
        if inspection.reference != f"{repository}@{inspection.descriptor.digest}":
            raise RuntimeError(
                "container-worker image index contains a descriptor from another repository"
            )

    attestations = [
        inspection for inspection in inspections if _is_buildkit_attestation(inspection.descriptor)
    ]
    runnable = [inspection for inspection in inspections if inspection not in attestations]
    if len(runnable) != 1 or not _is_linux_amd64_image(runnable[0].descriptor):
        raise RuntimeError(
            "container-worker image index must contain exactly one runnable Linux amd64 image"
        )
    runnable_digest = runnable[0].descriptor.digest
    # Only payloads that carry annotations can be cross-checked; `docker manifest
    # inspect --verbose` omits them, and an attestation it reported is already
    # known to belong to this index because every descriptor was matched to this
    # repository above.
    if any(
        attestation.descriptor.annotations.get(_DOCKER_REFERENCE_DIGEST_ANNOTATION, runnable_digest)
        != runnable_digest
        for attestation in attestations
    ):
        raise RuntimeError("container-worker image index contains an attestation for another image")


def _is_linux_amd64_image(descriptor: DockerImageDescriptor) -> bool:
    return (
        descriptor.media_type
        in {
            _OCI_IMAGE_MANIFEST_MEDIA_TYPE,
            _DOCKER_IMAGE_MANIFEST_MEDIA_TYPE,
        }
        and descriptor.platform.os == "linux"
        and descriptor.platform.architecture == "amd64"
    )


def _is_buildkit_attestation(descriptor: DockerImageDescriptor) -> bool:
    """Whether a descriptor is a buildkit attestation rather than a runnable image.

    `docker manifest inspect --verbose` omits descriptor annotations, so the
    reference-type annotation buildkit writes is not available here even though
    the registry index carries it. The `unknown/unknown` platform is what
    buildkit uses to keep attestations off every real platform and is present in
    both payloads; the annotations are still required when the payload includes
    them, so a genuine image cannot be discarded by claiming to be one.
    """
    if (
        descriptor.media_type != _OCI_IMAGE_MANIFEST_MEDIA_TYPE
        or descriptor.platform.os != "unknown"
        or descriptor.platform.architecture != "unknown"
    ):
        return False
    if not descriptor.annotations:
        return True
    return (
        descriptor.annotations.get(_DOCKER_REFERENCE_TYPE_ANNOTATION)
        == _DOCKER_ATTESTATION_REFERENCE_TYPE
        and _DOCKER_REFERENCE_DIGEST_ANNOTATION in descriptor.annotations
    )


def _publish_object(
    source: Path,
    release_object: ReleaseObject,
    *,
    bucket: str,
    region: str,
    aws_cli: str,
) -> None:
    _verify_file(
        source,
        expected_sha256=release_object.sha256,
        expected_size=release_object.size_bytes,
    )
    checksum = base64.b64encode(bytes.fromhex(release_object.sha256)).decode()
    existing = subprocess.run(
        [
            aws_cli,
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            release_object.object_key,
            "--checksum-mode",
            "ENABLED",
            "--region",
            region,
            "--output",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        payload = AwsHeadObjectResponse.model_validate_json(existing.stdout)
        if (
            payload.content_length != release_object.size_bytes
            or payload.checksum_sha256 != checksum
        ):
            raise RuntimeError(
                f"immutable AWS release object already exists with different bytes: "
                f"s3://{bucket}/{release_object.object_key}"
            )
    elif not _is_missing_s3_object(existing):
        raise RuntimeError(f"could not inspect AWS release object: {_command_error(existing)}")
    else:
        uploaded = subprocess.run(
            [
                aws_cli,
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                release_object.object_key,
                "--body",
                str(source),
                "--content-type",
                release_object.content_type,
                "--cache-control",
                release_object.cache_control,
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum,
                "--if-none-match",
                "*",
                "--region",
                region,
                "--output",
                "json",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if uploaded.returncode != 0:
            raise RuntimeError(f"could not publish AWS release object: {_command_error(uploaded)}")
    _verify_public_object(release_object)


def _release_object_path(root: Path, release_object: ReleaseObject) -> Path:
    path = (root / release_object.local_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("AWS release object path escapes the release bundle")
    return path


def _verify_public_object(release_object: ReleaseObject) -> bytes:
    payload = _download_public(release_object.public_url)
    if len(payload) != release_object.size_bytes or _sha256_bytes(payload) != release_object.sha256:
        raise RuntimeError(
            f"public AWS release object failed integrity verification: {release_object.public_url}"
        )
    return payload


def _download_public(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "lazycloud-release-verifier/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except (OSError, urllib.error.HTTPError) as exc:
        raise RuntimeError(f"AWS release object is not anonymously readable: {url}") from exc


def _is_missing_s3_object(result: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{result.stdout}\n{result.stderr}".casefold()
    return "not found" in detail or "404" in detail or "nosuchkey" in detail


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    return detail[-1000:] if detail else f"exit status {result.returncode}"


def _verify_file(path: Path, *, expected_sha256: str, expected_size: int) -> None:
    if not path.is_file():
        raise RuntimeError(f"AWS release artifact does not exist: {path}")
    if path.stat().st_size != expected_size or _sha256(path) != expected_sha256:
        raise RuntimeError(f"AWS release artifact failed integrity verification: {path}")


def _copy_immutable(source: Path, destination: Path) -> None:
    _write_immutable(destination, source.read_bytes())


def _write_immutable(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file() or destination.read_bytes() != payload:
            raise RuntimeError(f"immutable AWS release artifact already exists: {destination}")
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _manifest_payload(manifest: AwsReleaseManifest) -> bytes:
    return (
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _s3_public_url(bucket: str, region: str, object_key: str) -> str:
    encoded_key = "/".join(urllib.request.pathname2url(part) for part in object_key.split("/"))
    return f"https://s3.{region}.amazonaws.com/{bucket}/{encoded_key}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


if __name__ == "__main__":
    main()

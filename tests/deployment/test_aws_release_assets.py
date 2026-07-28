from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from provider_aws import aws_account_connection_template_identity
from pydantic import JsonValue, ValidationError

aws_release_assets = importlib.import_module("deploy.aws-release-assets.release")


def _agent_version_dir(
    root: Path,
    *,
    version: str,
    payload: bytes = b"\x7fELFstandalone-agent",
) -> Path:
    version_root = root / "agent-binarys" / version
    version_root.mkdir(parents=True)
    agent = version_root / "lazycloud-agent-linux-amd64"
    agent.write_bytes(payload)
    agent.chmod(0o755)
    digest = aws_release_assets._sha256(agent)
    (version_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": version,
                "artifacts": [
                    {
                        "os": "linux",
                        "arch": "amd64",
                        "filename": agent.name,
                        "sha256": digest,
                        "size_bytes": agent.stat().st_size,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return version_root


def _stage(root: Path, *, version: str = "release-123"):
    return aws_release_assets.stage_release(
        version=version,
        agent_version_dir=_agent_version_dir(root, version=version),
        worker_image=f"public.ecr.aws/example/worker@sha256:{'c' * 64}",
        bucket="release-assets-example",
        region="us-east-1",
        key_prefix="connected-aws",
        output=root / "release",
    )


def test_stage_release_binds_exact_immutable_runtime_assets(tmp_path: Path) -> None:
    manifest = _stage(tmp_path)
    template_identity = aws_account_connection_template_identity()

    assert manifest.connection_template_sha256 == template_identity.sha256
    assert manifest.agent_artifact_version == "release-123"
    assert manifest.container_worker_image.endswith(f"@sha256:{'c' * 64}")
    assert manifest.deployment_environment == {
        "LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH": json.dumps(
            {"amd64": manifest.agent_artifact_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ),
        "LAZYCLOUD_AGENT_BINARY_VERSION": "release-123",
        "LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL": next(
            item.public_url
            for item in manifest.objects
            if item.object_key.endswith(
                f"/agents/release-123/{manifest.agent_artifact_sha256}/lazycloud-agent-linux-amd64"
            )
        ),
        "LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST": manifest.container_worker_image,
        "LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL": next(
            item.public_url
            for item in manifest.objects
            if item.object_key.endswith(
                f"/connection-templates/{template_identity.sha256}/template.json"
            )
        ),
    }
    retained = aws_release_assets.validated_local_agent_artifact_root(
        tmp_path / "release" / "release-123" / "manifest.json",
        manifest,
    )
    assert retained == (tmp_path / "release" / "release-123" / "agent-binarys").resolve()
    assert (
        aws_release_assets.validate_local_release(
            tmp_path / "release" / "release-123" / "manifest.json",
            manifest,
        )
        == retained
    )


def test_stage_release_records_baked_cpu_ami_catalog(tmp_path: Path) -> None:
    version = "release-123"
    manifest = aws_release_assets.stage_release(
        version=version,
        agent_version_dir=_agent_version_dir(tmp_path, version=version),
        worker_image=f"public.ecr.aws/example/worker@sha256:{'c' * 64}",
        bucket="release-assets-example",
        region="us-east-1",
        key_prefix="connected-aws",
        output=tmp_path / "release",
        cpu_ami_ids={" US-East-1": "AMI-0123456789abcdef0"},
    )

    assert manifest.capacity_cpu_ami_ids == {"us-east-1": "ami-0123456789abcdef0"}
    assert manifest.deployment_environment["LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS"] == (
        '{"us-east-1":"ami-0123456789abcdef0"}'
    )
    reloaded = aws_release_assets.AwsReleaseManifest.model_validate_json(manifest.model_dump_json())
    assert reloaded == manifest

    tampered = manifest.model_dump(mode="json")
    tampered["capacity_cpu_ami_ids"] = {"us-east-1": "ami-not-a-real-id"}
    with pytest.raises(ValidationError, match="invalid AMI ID"):
        aws_release_assets.AwsReleaseManifest.model_validate(tampered)


def test_local_validation_rejects_tampered_release_object(tmp_path: Path) -> None:
    manifest = _stage(tmp_path)
    template = next(
        release_object
        for release_object in manifest.objects
        if release_object.object_key.endswith("/template.json")
    )
    (tmp_path / "release" / "release-123" / template.local_path).write_bytes(b"changed")

    with pytest.raises(RuntimeError, match="integrity verification"):
        aws_release_assets.validate_local_release(
            tmp_path / "release" / "release-123" / "manifest.json",
            manifest,
        )


def test_stage_release_rejects_non_elf_agent(tmp_path: Path) -> None:
    version = "release-123"
    version_root = _agent_version_dir(
        tmp_path,
        version=version,
        payload=b"not-an-elf",
    )

    with pytest.raises(ValueError, match="not an ELF executable"):
        aws_release_assets.stage_release(
            version=version,
            agent_version_dir=version_root,
            worker_image=f"public.ecr.aws/example/worker@sha256:{'c' * 64}",
            bucket="release-assets-example",
            region="us-east-1",
            key_prefix="connected-aws",
            output=tmp_path / "release",
        )


def test_stage_release_rejects_mutating_an_existing_version(tmp_path: Path) -> None:
    _stage(tmp_path, version="same-version")
    agent = tmp_path / "agent-binarys" / "same-version" / "lazycloud-agent-linux-amd64"
    agent.write_bytes(b"\x7fELFchanged-agent")
    manifest_path = tmp_path / "agent-binarys" / "same-version" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"][0]["sha256"] = aws_release_assets._sha256(agent)
    manifest["artifacts"][0]["size_bytes"] = agent.stat().st_size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="immutable"):
        aws_release_assets.stage_release(
            version="same-version",
            agent_version_dir=agent.parent,
            worker_image=f"public.ecr.aws/example/worker@sha256:{'c' * 64}",
            bucket="release-assets-example",
            region="us-east-1",
            key_prefix="connected-aws",
            output=tmp_path / "release",
        )


def test_load_release_rejects_a_manifest_from_obsolete_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage(tmp_path)
    manifest_path = tmp_path / "release" / "release-123" / "manifest.json"
    monkeypatch.setattr(
        aws_release_assets,
        "aws_account_connection_template_identity",
        lambda: SimpleNamespace(version="new.v1", sha256="d" * 64),
    )

    with pytest.raises(ValueError, match="current connection template"):
        aws_release_assets.load_release_manifest(manifest_path)


def test_worker_image_verification_requires_exact_linux_amd64_digest() -> None:
    digest = f"sha256:{'c' * 64}"
    reference = f"public.ecr.aws/example/worker@{digest}"
    payload = json.dumps(
        {
            "Ref": reference,
            "Descriptor": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": digest,
                "platform": {"architecture": "amd64", "os": "linux"},
            },
        }
    )

    aws_release_assets._validate_worker_image_platform(
        payload,
        expected_reference=reference,
    )

    arm_payload = json.dumps(
        {
            "Ref": reference,
            "Descriptor": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": digest,
                "platform": {"architecture": "arm64", "os": "linux"},
            },
        }
    )
    with pytest.raises(RuntimeError, match="exact Linux amd64"):
        aws_release_assets._validate_worker_image_platform(
            arm_payload,
            expected_reference=reference,
        )
    with pytest.raises(RuntimeError, match="immutable sha256 digest"):
        aws_release_assets._validate_worker_image_platform(
            payload,
            expected_reference="public.ecr.aws/example/worker:latest",
        )


def test_worker_image_verification_accepts_buildkit_attested_amd64_index() -> None:
    index_digest = f"sha256:{'a' * 64}"
    image_digest = f"sha256:{'b' * 64}"
    attestation_digest = f"sha256:{'c' * 64}"
    repository = "public.ecr.aws/example/worker"
    payload = json.dumps(
        [
            {
                "Ref": f"{repository}@{image_digest}",
                "Descriptor": {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": image_digest,
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
            },
            {
                "Ref": f"{repository}@{attestation_digest}",
                "Descriptor": {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": attestation_digest,
                    "platform": {"architecture": "unknown", "os": "unknown"},
                    "annotations": {
                        "vnd.docker.reference.digest": image_digest,
                        "vnd.docker.reference.type": "attestation-manifest",
                    },
                },
            },
        ]
    )

    aws_release_assets._validate_worker_image_platform(
        payload,
        expected_reference=f"{repository}@{index_digest}",
    )


@pytest.mark.parametrize(
    ("extra_descriptor", "error"),
    [
        (
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{'d' * 64}",
                "platform": {"architecture": "arm64", "os": "linux"},
            },
            "exactly one runnable Linux amd64",
        ),
        (
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{'d' * 64}",
                "platform": {"architecture": "unknown", "os": "unknown"},
            },
            "exactly one runnable Linux amd64",
        ),
        (
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{'d' * 64}",
                "platform": {"architecture": "unknown", "os": "unknown"},
                "annotations": {
                    "vnd.docker.reference.digest": f"sha256:{'e' * 64}",
                    "vnd.docker.reference.type": "attestation-manifest",
                },
            },
            "attestation for another image",
        ),
    ],
)
def test_worker_image_verification_rejects_ambiguous_index(
    extra_descriptor: dict[str, JsonValue],
    error: str,
) -> None:
    index_digest = f"sha256:{'a' * 64}"
    image_digest = f"sha256:{'b' * 64}"
    repository = "public.ecr.aws/example/worker"
    payload = json.dumps(
        [
            {
                "Ref": f"{repository}@{image_digest}",
                "Descriptor": {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": image_digest,
                    "platform": {"architecture": "amd64", "os": "linux"},
                },
            },
            {
                "Ref": f"{repository}@{extra_descriptor['digest']}",
                "Descriptor": extra_descriptor,
            },
        ]
    )

    with pytest.raises(RuntimeError, match=error):
        aws_release_assets._validate_worker_image_platform(
            payload,
            expected_reference=f"{repository}@{index_digest}",
        )

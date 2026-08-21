"""Bake per-region connected-AWS node AMIs for a release.

Launches one temporary Amazon Linux 2023 instance per region, installs the
release runtime dependencies (Docker, the pinned Tailscale build, the release
agent executable, the pre-pulled container-worker image) through user data,
stops the instance, and registers the immutable node image. The managed-pool
bootstrap script keeps every install step as a guarded no-op, so nodes booted
from a baked image skip straight to enrollment.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from urllib.request import pathname2url

from agent.operations import build_agent_install_script
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from shared.app_identity import AGENT_NAME
from shared.tailscale_install import TAILSCALE_INSTALL_VERSION

_AGENT_FILENAME = "lazycloud-agent-linux-amd64"
# One driver serves every card this fleet rents: the branch is unified from
# Turing through Blackwell, so the GPU image is one per region rather than one per
# model. Pinned rather than "latest" because gVisor's nvproxy validates the driver
# ABI it was built against, so the node driver, the gVisor release and this image
# are one decision.
#
# The exact build, not the branch. A branch resolves to whatever the CUDA repo
# published most recently, which is how a bake asking for "580" installed
# 580.178.04 — a build nvproxy does not know — and failed its own ABI check after
# paying for the whole driver install. This is the newest build carried by both
# the repo and the pinned gVisor; the repo goes on to 610.57.04 and nvproxy to
# 620.06.00, but they share nothing above this.
_NVIDIA_DRIVER_VERSION = "590.48.01"
_NVIDIA_DRIVER_RELEASE = "1.amzn2023"
# Amazon Linux 2023 carries no `nvidia-driver:<branch>` stream: the CUDA repo
# names its streams `<branch>-open` and `<branch>-dkms`, and asking for the bare
# branch fails with "missing groups or modules". Open kernel modules cover Turing
# onward, which is every card in the catalog.
_NVIDIA_DRIVER_STREAM = "590-open"
# Every build whose ABI the pinned gVisor knows, as `runsc nvproxy
# list-supported-drivers` reports it. The pin above should make this unreachable;
# it stays because the pin is a request to a repository that can stop honouring
# it, and shipping an image whose sandbox refuses every GPU container is the
# failure it exists to prevent. Regenerate with:
#
#   docker run --rm --entrypoint runsc container-worker:local \
#     nvproxy list-supported-drivers
#
# Keep in step with GVISOR_VERSION in docker/Dockerfile.worker.
_NVPROXY_SUPPORTED_DRIVERS = (
    "535.129.03",
    "535.183.06",
    "535.247.01",
    "535.261.03",
    "535.274.02",
    "535.288.01",
    "535.309.01",
    "550.90.12",
    "570.124.06",
    "570.133.20",
    "570.172.08",
    "570.195.03",
    "580.65.06",
    "580.105.08",
    "580.126.09",
    "580.126.20",
    "580.159.03",
    "580.159.04",
    "580.173.02",
    "590.48.01",
    "615.15.00",
    "620.06.00",
)
# A GPU bake must run on a GPU or it cannot check its own work; this is the
# cheapest instance that has one.
_GPU_BAKE_INSTANCE_TYPE = "g4dn.xlarge"
# Not burstable. The bake's cost is a driver install and a container-worker pull,
# both of which are exactly what exhausts a `t3` credit balance, and a bake whose
# duration depends on how much credit the account happened to have is one whose
# timeout means nothing. A few cents an hour buys a run that takes the same time
# every time.
_CPU_BAKE_INSTANCE_TYPE = "c7i.large"
# The driver and CUDA userspace do not fit in the CPU image's 16 GiB.
_GPU_ROOT_VOLUME_GIB = 40
_CPU_ROOT_VOLUME_GIB = 16
# EC2 rejects a launch whose base64-encoded user data is larger than this.
_MAX_ENCODED_USER_DATA_BYTES = 25600
_AL2023_SSM_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
_AMI_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_BAKE_FAILED_SENTINEL = "LAZYCLOUD_BAKE_FAILED"
_BAKE_OK_SENTINEL = "LAZYCLOUD_BAKE_OK"
_CLI_TIMEOUT_SECONDS = 300
_CONSOLE_SETTLE_ATTEMPTS = 4
_CONSOLE_TAIL_LINES = 40
_MANAGED_TAG_KEY = "cloud-pool:managed-by"
_MANAGED_TAG_VALUE = "control-plane"
_POLL_INTERVAL_SECONDS = 15
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_RELEASE_TAG_KEY = "lazycloud:release"
_S3_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_WORKER_IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$")


class _BakeModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _AgentArtifactEntry(_BakeModel):
    os: str
    arch: str
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)


class _AgentArtifactManifest(_BakeModel):
    schema_version: int
    version: str
    artifacts: list[_AgentArtifactEntry]


class _SsmParameter(_BakeModel):
    value: str = Field(alias="Value")


class _GetParameterResponse(_BakeModel):
    parameter: _SsmParameter = Field(alias="Parameter")


class _DescribedImage(_BakeModel):
    image_id: str = Field(alias="ImageId")
    state: str = Field(alias="State")


class _DescribeImagesResponse(_BakeModel):
    images: list[_DescribedImage] = Field(default_factory=list, alias="Images")


class _InstanceState(_BakeModel):
    name: str = Field(alias="Name")


class _DescribedInstance(_BakeModel):
    instance_id: str = Field(alias="InstanceId")
    state: _InstanceState = Field(alias="State")


class _Reservation(_BakeModel):
    instances: list[_DescribedInstance] = Field(default_factory=list, alias="Instances")


class _DescribeInstancesResponse(_BakeModel):
    reservations: list[_Reservation] = Field(default_factory=list, alias="Reservations")


class _RunInstancesResponse(_BakeModel):
    instances: list[_DescribedInstance] = Field(alias="Instances")


class _CreateImageResponse(_BakeModel):
    image_id: str = Field(alias="ImageId")


class _HeadObjectResponse(_BakeModel):
    content_length: int = Field(alias="ContentLength")
    checksum_sha256: str = Field(alias="ChecksumSHA256")


@dataclass(frozen=True, slots=True)
class _AgentArtifact:
    path: Path
    sha256: str
    size_bytes: int


class _BakeVariant(StrEnum):
    Cpu = "cpu"
    Gpu = "gpu"


@dataclass(frozen=True, slots=True)
class _BakeRequest:
    variant: _BakeVariant
    release_version: str
    agent: _AgentArtifact
    agent_url: str
    worker_image: str
    instance_type: str
    instance_profile: str | None
    subnet_id: str | None
    instance_timeout_seconds: int
    image_timeout_seconds: int
    aws_cli: str

    @property
    def image_name(self) -> str:
        """Distinct per variant, because reuse is silently wrong.

        An existing image is matched by name alone, so a GPU bake sharing the CPU
        name would find that image `available` and return it — publishing a
        driverless AMI as the GPU catalog entry.
        """
        suffix = "-gpu" if self.variant is _BakeVariant.Gpu else ""
        return f"lazycloud-node-{self.release_version}{suffix}-amd64"

    @property
    def root_volume_gib(self) -> int:
        return _GPU_ROOT_VOLUME_GIB if self.variant is _BakeVariant.Gpu else _CPU_ROOT_VOLUME_GIB


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Bake immutable per-region connected-AWS node AMIs "
            "(Docker + pinned Tailscale + release agent + pre-pulled worker image)."
        )
    )
    parser.add_argument(
        "--variant",
        choices=[variant.value for variant in _BakeVariant],
        default=_BakeVariant.Cpu.value,
        help="cpu bakes the default node image; gpu adds the NVIDIA driver and toolkit",
    )
    parser.add_argument("--release-version", required=True)
    parser.add_argument(
        "--agent-version-dir",
        type=Path,
        required=True,
        help="staged agent artifact directory containing manifest.json (deploy/agent-binary)",
    )
    parser.add_argument("--worker-image", required=True)
    parser.add_argument("--bucket", required=True, help="public release asset bucket")
    parser.add_argument("--bucket-region", required=True)
    parser.add_argument("--key-prefix", default="connected-aws")
    parser.add_argument("--regions", nargs="+", default=["us-east-1"])
    parser.add_argument(
        "--instance-type",
        default=None,
        help="defaults to t3.small for cpu and a GPU instance for gpu",
    )
    parser.add_argument("--instance-profile", default=None)
    parser.add_argument("--subnet-id", default=None)
    parser.add_argument("--instance-timeout-seconds", type=int, default=1500)
    parser.add_argument("--image-timeout-seconds", type=int, default=1800)
    parser.add_argument("--aws-cli", default="aws")
    args = parser.parse_args()

    version: str = args.release_version
    if not _VERSION_PATTERN.fullmatch(version):
        raise SystemExit("release version contains invalid characters")
    if not _WORKER_IMAGE_PATTERN.fullmatch(args.worker_image):
        raise SystemExit("worker image must use an immutable sha256 digest reference")
    if not _S3_NAME_PATTERN.fullmatch(args.bucket):
        raise SystemExit("invalid release bucket name")
    regions = [region.strip().lower() for region in args.regions]
    for region in [args.bucket_region, *regions]:
        if not _REGION_PATTERN.fullmatch(region):
            raise SystemExit(f"invalid AWS region: {region}")
    if len(regions) != len(set(regions)):
        raise SystemExit("bake regions must be unique")
    if args.instance_timeout_seconds <= 0 or args.image_timeout_seconds <= 0:
        raise SystemExit("bake timeouts must be positive")

    key_prefix = args.key_prefix.strip().strip("/")
    if not key_prefix or any(part in {".", ".."} for part in key_prefix.split("/")):
        raise SystemExit("release key prefix is invalid")

    agent = _load_agent_artifact(args.agent_version_dir, version=version)
    agent_key = f"{key_prefix}/agents/{version}/{agent.sha256}/{_AGENT_FILENAME}"
    agent_url = _s3_public_url(args.bucket, args.bucket_region, agent_key)
    _ensure_agent_published(
        agent,
        bucket=args.bucket,
        region=args.bucket_region,
        object_key=agent_key,
        public_url=agent_url,
        aws_cli=args.aws_cli,
    )

    variant = _BakeVariant(args.variant)
    request = _BakeRequest(
        variant=variant,
        release_version=version,
        agent=agent,
        agent_url=agent_url,
        worker_image=args.worker_image,
        instance_type=args.instance_type or _default_bake_instance_type(variant),
        instance_profile=args.instance_profile,
        subnet_id=args.subnet_id,
        instance_timeout_seconds=args.instance_timeout_seconds,
        image_timeout_seconds=args.image_timeout_seconds,
        aws_cli=args.aws_cli,
    )
    ami_ids = {region: _bake_region(request, region=region) for region in regions}
    print(json.dumps(ami_ids, sort_keys=True, separators=(",", ":")))


def _load_agent_artifact(version_dir: Path, *, version: str) -> _AgentArtifact:
    manifest_path = version_dir.resolve() / "manifest.json"
    try:
        manifest = _AgentArtifactManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise SystemExit(f"invalid agent artifact manifest: {manifest_path}") from exc
    if manifest.schema_version != 1 or manifest.version != version:
        raise SystemExit("agent artifact manifest does not match the release version")
    entries = [
        entry
        for entry in manifest.artifacts
        if entry.os == "linux" and entry.arch == "amd64" and entry.filename == _AGENT_FILENAME
    ]
    if len(entries) != 1:
        raise SystemExit("agent artifact manifest must contain exactly one linux/amd64 artifact")
    entry = entries[0]
    path = version_dir.resolve() / entry.filename
    if not path.is_file() or path.stat().st_size != entry.size_bytes:
        raise SystemExit(f"agent artifact failed integrity verification: {path}")
    digest = sha256()
    with path.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != entry.sha256:
        raise SystemExit(f"agent artifact failed integrity verification: {path}")
    return _AgentArtifact(path=path, sha256=entry.sha256, size_bytes=entry.size_bytes)


def _ensure_agent_published(
    agent: _AgentArtifact,
    *,
    bucket: str,
    region: str,
    object_key: str,
    public_url: str,
    aws_cli: str,
) -> None:
    """Publish the immutable sha-addressed agent object the bake instances download.

    The release bucket serves anonymous reads, and the object key embeds the
    artifact digest, so publishing before `release.py publish` yields the exact
    object the release manifest later references; the release publisher verifies
    byte identity against any existing object.
    """
    checksum = b64encode(bytes.fromhex(agent.sha256)).decode()
    existing = _run_aws(
        aws_cli,
        [
            "s3api",
            "head-object",
            "--bucket",
            bucket,
            "--key",
            object_key,
            "--checksum-mode",
            "ENABLED",
            "--region",
            region,
            "--output",
            "json",
        ],
        check=False,
    )
    if existing.returncode == 0:
        head = _parse(_HeadObjectResponse, existing.stdout, operation="inspect agent object")
        if head.content_length != agent.size_bytes or head.checksum_sha256 != checksum:
            raise SystemExit(
                f"immutable agent object already exists with different bytes: "
                f"s3://{bucket}/{object_key}"
            )
    else:
        detail = f"{existing.stdout}\n{existing.stderr}".casefold()
        if not ("not found" in detail or "404" in detail or "nosuchkey" in detail):
            raise SystemExit(f"could not inspect agent object: {_command_error(existing)}")
        _log(f"publishing agent object s3://{bucket}/{object_key}")
        uploaded = _run_aws(
            aws_cli,
            [
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                object_key,
                "--body",
                str(agent.path),
                "--content-type",
                "application/octet-stream",
                "--cache-control",
                "public, max-age=31536000, immutable",
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
        )
        if uploaded.returncode != 0:
            raise SystemExit(f"could not publish agent object: {_command_error(uploaded)}")
    _verify_public_agent(public_url, expected_sha256=agent.sha256, expected_size=agent.size_bytes)


def _verify_public_agent(url: str, *, expected_sha256: str, expected_size: int) -> None:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": "lazycloud-ami-bake/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except (OSError, urllib.error.HTTPError) as exc:
        raise SystemExit(f"agent object is not anonymously readable: {url}") from exc
    if len(payload) != expected_size or sha256(payload).hexdigest() != expected_sha256:
        raise SystemExit(f"public agent object failed integrity verification: {url}")


def _bake_region(request: _BakeRequest, *, region: str) -> str:
    existing = _find_existing_image(request, region=region)
    if existing is not None:
        if existing.state == "available":
            _log(f"{region}: image {request.image_name} already available as {existing.image_id}")
            return existing.image_id
        if existing.state == "pending":
            _log(f"{region}: image {request.image_name} already pending as {existing.image_id}")
            _wait_for_image(request, region=region, image_id=existing.image_id)
            return existing.image_id
        raise SystemExit(
            f"{region}: image {request.image_name} exists in unusable state {existing.state}"
        )

    base_ami = _latest_al2023_ami(request, region=region)
    _log(f"{region}: baking {request.image_name} from {base_ami}")
    instance_id = _launch_bake_instance(request, region=region, base_ami=base_ami)
    try:
        _wait_for_instance_stopped(request, region=region, instance_id=instance_id)
        image_id = _create_image(request, region=region, instance_id=instance_id)
        _wait_for_image(request, region=region, image_id=image_id)
    finally:
        _terminate_instance(request, region=region, instance_id=instance_id)
    _log(f"{region}: baked {image_id}")
    return image_id


def _find_existing_image(request: _BakeRequest, *, region: str) -> _DescribedImage | None:
    result = _run_aws(
        request.aws_cli,
        [
            "ec2",
            "describe-images",
            "--owners",
            "self",
            "--filters",
            f"Name=name,Values={request.image_name}",
            "--region",
            region,
            "--output",
            "json",
        ],
    )
    images = _parse(_DescribeImagesResponse, result.stdout, operation="describe images").images
    if not images:
        return None
    if len(images) > 1:
        raise SystemExit(f"{region}: multiple images named {request.image_name}")
    return images[0]


def _latest_al2023_ami(request: _BakeRequest, *, region: str) -> str:
    result = _run_aws(
        request.aws_cli,
        [
            "ssm",
            "get-parameter",
            "--name",
            _AL2023_SSM_PARAMETER,
            "--region",
            region,
            "--output",
            "json",
        ],
    )
    ami_id = _parse(
        _GetParameterResponse, result.stdout, operation="resolve AL2023 AMI"
    ).parameter.value.strip()
    if not _AMI_PATTERN.fullmatch(ami_id):
        raise SystemExit(f"{region}: SSM returned an invalid AL2023 AMI ID")
    return ami_id


def _launch_bake_instance(request: _BakeRequest, *, region: str, base_ami: str) -> str:
    tags = [
        {"Key": "Name", "Value": f"lazycloud-ami-bake-{request.release_version}"},
        {"Key": _MANAGED_TAG_KEY, "Value": _MANAGED_TAG_VALUE},
        {"Key": _RELEASE_TAG_KEY, "Value": request.release_version},
    ]
    tag_specifications = json.dumps(
        [
            {"ResourceType": "instance", "Tags": tags},
            {"ResourceType": "volume", "Tags": tags},
        ],
        separators=(",", ":"),
    )
    block_device_mappings = json.dumps(
        [
            {
                "DeviceName": "/dev/xvda",
                "Ebs": {
                    "VolumeSize": request.root_volume_gib,
                    "VolumeType": "gp3",
                    "Encrypted": True,
                    "DeleteOnTermination": True,
                },
            }
        ],
        separators=(",", ":"),
    )
    with tempfile.NamedTemporaryFile("wb", suffix=".sh.gz") as user_data:
        user_data.write(_bake_user_data_blob(request))
        user_data.flush()
        command = [
            "ec2",
            "run-instances",
            "--image-id",
            base_ami,
            "--instance-type",
            request.instance_type,
            "--count",
            "1",
            "--instance-initiated-shutdown-behavior",
            "stop",
            "--metadata-options",
            "HttpEndpoint=enabled,HttpTokens=required,HttpPutResponseHopLimit=1",
            "--block-device-mappings",
            block_device_mappings,
            "--tag-specifications",
            tag_specifications,
            "--user-data",
            f"fileb://{user_data.name}",
            "--region",
            region,
            "--output",
            "json",
        ]
        if request.instance_profile is not None:
            command.extend(["--iam-instance-profile", f"Name={request.instance_profile}"])
        if request.subnet_id is not None:
            command.extend(["--subnet-id", request.subnet_id])
        result = _run_aws(request.aws_cli, command)
    instances = _parse(_RunInstancesResponse, result.stdout, operation="launch bake instance")
    if len(instances.instances) != 1:
        raise SystemExit(f"{region}: AWS did not launch exactly one bake instance")
    instance_id = instances.instances[0].instance_id
    _log(f"{region}: launched bake instance {instance_id}")
    return instance_id


def _read_console(request: _BakeRequest, *, region: str, instance_id: str) -> str:
    """What the instance has said so far, or nothing while EC2 catches up.

    Console output lags the instance by a minute or two and is empty until the
    first flush, so absence here is never evidence of silence.
    """
    result = _run_aws(
        request.aws_cli,
        [
            "ec2",
            "get-console-output",
            "--instance-id",
            instance_id,
            "--region",
            region,
            "--output",
            "text",
            "--query",
            "Output",
        ],
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def _wait_for_instance_stopped(request: _BakeRequest, *, region: str, instance_id: str) -> None:
    deadline = time.monotonic() + request.instance_timeout_seconds
    announced_ok = False
    while True:
        console = _read_console(request, region=region, instance_id=instance_id)
        if _BAKE_FAILED_SENTINEL in console:
            tail = "\n".join(console.strip().splitlines()[-_CONSOLE_TAIL_LINES:])
            raise SystemExit(
                f"{region}: the bake script failed on {instance_id}. Its last "
                f"{_CONSOLE_TAIL_LINES} console lines:\n{tail}"
            )
        if not announced_ok and _BAKE_OK_SENTINEL in console:
            announced_ok = True
            _log(f"{region}: bake script finished on {instance_id}, waiting for it to stop")
        result = _run_aws(
            request.aws_cli,
            [
                "ec2",
                "describe-instances",
                "--instance-ids",
                instance_id,
                "--region",
                region,
                "--output",
                "json",
            ],
            check=False,
        )
        if result.returncode != 0:
            # A freshly launched instance is eventually consistent: describe can
            # return NotFound for a few seconds after RunInstances succeeds.
            if "InvalidInstanceID.NotFound" in result.stderr and time.monotonic() < deadline:
                time.sleep(5)
                continue
            raise SystemExit(
                f"{region}: describe bake instance failed: {result.stderr.strip()[:500]}"
            )
        described = _parse(
            _DescribeInstancesResponse, result.stdout, operation="describe bake instance"
        )
        states = [
            instance.state.name
            for reservation in described.reservations
            for instance in reservation.instances
            if instance.instance_id == instance_id
        ]
        if len(states) != 1:
            raise SystemExit(f"{region}: bake instance {instance_id} was not observable")
        state = states[0]
        if state == "stopped":
            if announced_ok:
                return
            # The console trails the instance, so a stop seen before the success
            # line is usually only that lag. Give it a bounded chance to arrive
            # rather than imaging on the strength of the state alone.
            for _ in range(_CONSOLE_SETTLE_ATTEMPTS):
                time.sleep(_POLL_INTERVAL_SECONDS)
                console = _read_console(request, region=region, instance_id=instance_id)
                if _BAKE_OK_SENTINEL in console:
                    return
                if _BAKE_FAILED_SENTINEL in console:
                    break
            tail = "\n".join(console.strip().splitlines()[-_CONSOLE_TAIL_LINES:])
            raise SystemExit(
                f"{region}: bake instance {instance_id} stopped without finishing its "
                f"script. Its last {_CONSOLE_TAIL_LINES} console lines:\n{tail}"
            )
        if state in {"shutting-down", "terminated"}:
            raise SystemExit(f"{region}: bake instance {instance_id} terminated before imaging")
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"{region}: bake instance {instance_id} did not stop within "
                f"{request.instance_timeout_seconds}s (state {state}); "
                "the bake user data likely failed"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def _create_image(request: _BakeRequest, *, region: str, instance_id: str) -> str:
    tags = (
        f"{{Key={_MANAGED_TAG_KEY},Value={_MANAGED_TAG_VALUE}}},"
        f"{{Key={_RELEASE_TAG_KEY},Value={request.release_version}}}"
    )
    result = _run_aws(
        request.aws_cli,
        [
            "ec2",
            "create-image",
            "--instance-id",
            instance_id,
            "--name",
            request.image_name,
            "--description",
            f"LazyCloud connected-AWS node image for release {request.release_version}",
            "--tag-specifications",
            f"ResourceType=image,Tags=[{tags}]",
            f"ResourceType=snapshot,Tags=[{tags}]",
            "--region",
            region,
            "--output",
            "json",
        ],
    )
    image_id = _parse(_CreateImageResponse, result.stdout, operation="create node image").image_id
    if not _AMI_PATTERN.fullmatch(image_id):
        raise SystemExit(f"{region}: AWS returned an invalid image ID")
    return image_id


def _wait_for_image(request: _BakeRequest, *, region: str, image_id: str) -> None:
    deadline = time.monotonic() + request.image_timeout_seconds
    while True:
        result = _run_aws(
            request.aws_cli,
            [
                "ec2",
                "describe-images",
                "--image-ids",
                image_id,
                "--region",
                region,
                "--output",
                "json",
            ],
        )
        images = _parse(_DescribeImagesResponse, result.stdout, operation="describe node image")
        if len(images.images) != 1 or images.images[0].image_id != image_id:
            raise SystemExit(f"{region}: node image {image_id} was not observable")
        state = images.images[0].state
        if state == "available":
            return
        if state not in {"pending"}:
            raise SystemExit(f"{region}: node image {image_id} entered state {state}")
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"{region}: node image {image_id} was not available within "
                f"{request.image_timeout_seconds}s"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def _terminate_instance(request: _BakeRequest, *, region: str, instance_id: str) -> None:
    result = _run_aws(
        request.aws_cli,
        [
            "ec2",
            "terminate-instances",
            "--instance-ids",
            instance_id,
            "--region",
            region,
            "--output",
            "json",
        ],
        check=False,
    )
    if result.returncode != 0:
        _log(f"{region}: could not terminate bake instance {instance_id}: {_command_error(result)}")
    else:
        _log(f"{region}: terminated bake instance {instance_id}")


_BAKE_USER_DATA_TEMPLATE = """#!/bin/bash
set -Eeuo pipefail

# To the console as well as the log. A bake instance is launched with no key
# pair and no instance profile, so it has neither SSH nor SSM for its whole
# life: a log on its disk is written where nothing can ever read it, and the
# baker is left inferring a cause from an instance that simply stopped moving.
exec > >(tee -a /var/log/lazycloud-bake.log > /dev/console) 2>&1

# Two sentinels the baker polls for, because the states EC2 reports cannot tell
# these apart. A script that fails never reaches `shutdown`, so the instance
# stays `running` exactly as it does while a driver installs, and the only thing
# that eventually distinguishes them is a timeout that explains nothing.
bake_failed() {
  echo "LAZYCLOUD_BAKE_FAILED rc=$? line=${BASH_LINENO[0]} cmd=${BASH_COMMAND}"
}
trap bake_failed ERR

WORKER_IMAGE_DIGEST=__WORKER_IMAGE_DIGEST__
RELEASE_VERSION=__RELEASE_VERSION__
AGENT_SHA256=__AGENT_SHA256__
TAILSCALE_VERSION=__TAILSCALE_VERSION__

# The installer this image was built from, embedded rather than fetched: a bake
# has no control plane to ask, and an image whose runtime came from a different
# installer than the one shipped alongside it is exactly the drift this
# replaced.
cat > /tmp/lazycloud-agent-install.sh <<'INSTALLER_EOF'
__INSTALL_SCRIPT__
INSTALLER_EOF

sh /tmp/lazycloud-agent-install.sh --install-only --agent-url __AGENT_BINARY_URL__
rm -f /tmp/lazycloud-agent-install.sh

__GPU_SETUP__
__ZRAM_SETUP__
systemctl enable --now amazon-ssm-agent
# A pool node produces no console output and reports nothing once its agent
# cannot reach the control plane. Without SSM every failure in that window is
# silent, so a bake that cannot offer it is not worth shipping.
systemctl is-enabled amazon-ssm-agent

docker pull "$WORKER_IMAGE_DIGEST"

cat > /etc/lazycloud-node-image.json <<MARKER
{"release_version":"${RELEASE_VERSION}","agent_sha256":"${AGENT_SHA256}","tailscale_version":"${TAILSCALE_VERSION}","worker_image":"${WORKER_IMAGE_DIGEST}","ssm_agent":true,"variant":"__VARIANT__"}
MARKER

# Last, and the baker will not image an instance that never said it. A stop is
# not evidence of a finished bake -- a spot reclaim, an operator, or a panic all
# stop an instance too, and every one of them would otherwise be captured and
# published as a node image.
echo "LAZYCLOUD_BAKE_OK release=${RELEASE_VERSION} variant=__VARIANT__"
sync
shutdown -h now
"""


_ZRAM_SETUP_FRAGMENT = """
# Compressed swap, and the reason the container memory ceilings mean anything.
#
# A cgroup holding anonymous pages with no swap has nothing reclaimable, so
# `memory.high` stops throttling and starts stalling: measured at 2ms per 8MiB
# allocation below the threshold and 21s per 8MiB above it, with the kernel
# scanning zero pages because there was nowhere to put them. With swap the same
# allocation slows by about 3x and the cgroup holds at its limit. `memory.low`
# protection has the same dependency, for the same reason.
#
# A quarter of RAM, not half: zram's backing store is RAM, so a device holding
# incompressible pages costs its full size in real memory. A quarter is enough
# for reclaim to have somewhere to go without the machine losing half itself.
#
# In RAM rather than on the root volume because the CPU catalog is EBS-only.
#
# Configured through the zram-generator AL2023 already ships rather than through
# a unit of our own. The generator owns zram0: it runs on every daemon-reload,
# loads the module, and drives `systemd-zram-setup@zram0`. A second unit doing
# its own modprobe races it for the same device and loses, and the bake dies
# with the device added and no swap on it.
#
# `host-memory-limit` is the setting that decides it. The shipped default sets up
# zram only on hosts under 800MB, which no node in the catalog is, so without
# this the generator declines and the rest of the file never takes effect.
# `none` removes the cap; a value in /etc wins over the one in /usr.
#
# No `compression-algorithm`. Asking for zstd on this kernel gets "algorithm
# zstd not recognised" and the default silently anyway, so naming it bought a
# warning and the illusion of a choice. Which algorithms exist is a property of
# the kernel zram was built into, so the bake prints the list rather than a
# preference written from somewhere that cannot see it.
cat > /etc/systemd/zram-generator.conf <<'ZRAM_EOF'
[zram0]
zram-size = ram / 4
# Above any disk swap, so reclaim compresses before it ever reaches a volume.
swap-priority = 100
host-memory-limit = none
ZRAM_EOF

# Reclaim has to prefer compressing a cold anonymous page over evicting a hot
# file page. The kernel's default assumes swap is a slow disk; this one is RAM.
cat > /etc/sysctl.d/60-lazycloud-zram.conf <<'SYSCTL_EOF'
vm.swappiness = 180
vm.page-cluster = 0
SYSCTL_EOF

# Prove it here rather than on a node with a tenant on it. A bake that cannot
# raise swap produces an image where every memory ceiling silently stalls
# instead of throttling.
systemctl daemon-reload
systemctl start systemd-zram-setup@zram0.service
grep -q '^/dev/zram0 ' /proc/swaps
cat /sys/block/zram0/comp_algorithm
"""


_GPU_SETUP_FRAGMENT = """
# Drivers first: nvidia-container-toolkit configures a docker runtime that cannot
# work without them, and a node whose toolkit registered against no driver reports
# healthy and fails every GPU container.
dnf install -y dnf-plugins-core
dnf config-manager --add-repo \
  https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo
dnf module enable -y nvidia-driver:__NVIDIA_DRIVER_STREAM__
dnf install -y nvidia-open-3:__NVIDIA_DRIVER_VERSION__-__NVIDIA_DRIVER_RELEASE__
curl -fsSL -o /etc/yum.repos.d/nvidia-container-toolkit.repo \
  https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo
dnf install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker

# Prove the image before it is registered. A GPU AMI that cannot see its own card
# otherwise ships, launches, enrols, reports no GPUs, and is marked unschedulable
# while billing — a failure that surfaces hours later and nowhere near the bake.
nvidia-smi -L
docker info --format '{{json .Runtimes}}' | grep -q nvidia

# The sandbox is the reason this image exists, and gVisor's driver proxy accepts
# only ABIs it was built against. Catching a mismatch here costs one bake;
# catching it later means every GPU container on the fleet fails to start.
INSTALLED_DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader \
  | head -1 | tr -d '[:space:]')"
echo "installed NVIDIA driver: ${INSTALLED_DRIVER}"
case " __NVPROXY_SUPPORTED_DRIVERS__ " in
  *" ${INSTALLED_DRIVER} "*) ;;
  *)
    echo "driver ${INSTALLED_DRIVER} is not an ABI the pinned gVisor proxies" >&2
    echo "supported: __NVPROXY_SUPPORTED_DRIVERS__" >&2
    exit 1
    ;;
esac
"""


def _default_bake_instance_type(variant: _BakeVariant) -> str:
    """A GPU bake must run where it can see a GPU, or it cannot verify itself."""
    return _GPU_BAKE_INSTANCE_TYPE if variant is _BakeVariant.Gpu else _CPU_BAKE_INSTANCE_TYPE


def _bake_user_data_blob(request: _BakeRequest) -> bytes:
    """Compress the bake script, which no longer fits in user data uncompressed.

    cloud-init decompresses gzipped user data before dispatching it, so the
    instance runs the same script either way. The agent installer the script
    embeds is on its own past EC2's limit, and compressing it keeps one source
    of truth for that installer rather than splitting it across a second fetch
    the bake would then have to publish and verify.
    """
    script = _bake_user_data(request)
    _reject_unparsable_script(script)
    blob = gzip.compress(script.encode("utf-8"), mtime=0)
    encoded = len(b64encode(blob))
    if encoded > _MAX_ENCODED_USER_DATA_BYTES:
        msg = (
            f"bake user data is {encoded} bytes encoded, over EC2's "
            f"{_MAX_ENCODED_USER_DATA_BYTES}: the embedded agent installer has outgrown "
            "user data and must move to a fetched artifact"
        )
        raise SystemExit(msg)
    return blob


def _reject_unparsable_script(script: str) -> None:
    """Parse the script here rather than discovering it will not parse on EC2.

    The script is assembled from fragments and substitutions, so a quoting
    mistake in any of them produces a file that only fails once an instance has
    booted it — and the failure arrives as an instance that stopped moving,
    minutes and one launch later. `bash -n` costs milliseconds and reads the
    exact bytes EC2 would.
    """
    result = subprocess.run(
        ["bash", "-n", "-"],
        input=script,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        msg = f"the generated bake script is not valid bash: {result.stderr.strip()[:500]}"
        raise SystemExit(msg)


def _bake_user_data(request: _BakeRequest) -> str:
    values = {
        "__TAILSCALE_VERSION__": TAILSCALE_INSTALL_VERSION,
        "__AGENT_BINARY_URL__": request.agent_url,
        "__AGENT_SHA256__": request.agent.sha256,
        "__WORKER_IMAGE_DIGEST__": request.worker_image,
        "__RELEASE_VERSION__": request.release_version,
        "__VARIANT__": request.variant.value,
    }
    script = _BAKE_USER_DATA_TEMPLATE
    gpu_setup = (
        _GPU_SETUP_FRAGMENT.replace("__NVIDIA_DRIVER_STREAM__", _NVIDIA_DRIVER_STREAM)
        .replace("__NVIDIA_DRIVER_VERSION__", _NVIDIA_DRIVER_VERSION)
        .replace("__NVIDIA_DRIVER_RELEASE__", _NVIDIA_DRIVER_RELEASE)
        .replace("__NVPROXY_SUPPORTED_DRIVERS__", " ".join(_NVPROXY_SUPPORTED_DRIVERS))
        if request.variant is _BakeVariant.Gpu
        else ""
    )
    script = script.replace("__GPU_SETUP__", gpu_setup)
    # Every variant: a GPU node's containers reclaim the same way a CPU node's do.
    script = script.replace("__ZRAM_SETUP__", _ZRAM_SETUP_FRAGMENT)
    for placeholder, value in values.items():
        script = script.replace(placeholder, shlex.quote(value))
    installer = build_agent_install_script(
        binary_name=AGENT_NAME,
        artifact_version=request.release_version,
        sha256_by_arch={"amd64": request.agent.sha256},
    )
    if "INSTALLER_EOF" in installer:
        msg = "agent install script contains the bake heredoc terminator"
        raise SystemExit(msg)
    # Substituted last and unquoted: it is a heredoc body, not a shell word.
    return script.replace("__INSTALL_SCRIPT__", installer)


def _run_aws(
    aws_cli: str,
    arguments: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [aws_cli, *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_SECONDS,
        env={**os.environ, "AWS_PAGER": ""},
    )
    if check and result.returncode != 0:
        operation = " ".join(arguments[:2])
        raise SystemExit(f"aws {operation} failed: {_command_error(result)}")
    return result


def _parse[ModelT: BaseModel](model: type[ModelT], payload: str, *, operation: str) -> ModelT:
    try:
        return model.model_validate_json(payload)
    except ValidationError as exc:
        raise SystemExit(f"AWS returned an invalid response while trying to {operation}") from exc


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = result.stderr.strip() or result.stdout.strip()
    return detail[-1000:] if detail else f"exit status {result.returncode}"


def _s3_public_url(bucket: str, region: str, object_key: str) -> str:
    encoded_key = "/".join(pathname2url(part) for part in object_key.split("/"))
    return f"https://s3.{region}.amazonaws.com/{bucket}/{encoded_key}"


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()

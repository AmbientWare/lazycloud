"""Publish and resolve the connected-AWS host-image catalog."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.request import pathname2url

from deploy.ami.recipe import host_recipe_sha256
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

_AMI_PATTERN = re.compile(r"^ami-[0-9a-f]{8,17}$")
_REGION_PATTERN = re.compile(r"^(us-gov|us|af|ap|ca|cn|eu|il|me|mx|sa)-[a-z0-9-]+-[0-9]+$")
_S3_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_RECIPE_TAG_KEY = "lazycloud:node-recipe"
_VARIANT_TAG_KEY = "lazycloud:node-variant"
_MANAGED_TAG_KEY = "cloud-pool:managed-by"
_MANAGED_TAG_VALUE = "control-plane"
_CLI_TIMEOUT_SECONDS = 300


class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _AwsModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class NodeImageCatalog(_CatalogModel):
    schema_version: Literal[1] = 1
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{7,64}$")
    cpu_ami_ids: dict[str, str]
    gpu_ami_ids: dict[str, str]

    @model_validator(mode="after")
    def validate_catalogs(self) -> NodeImageCatalog:
        for name, catalog in (("CPU", self.cpu_ami_ids), ("GPU", self.gpu_ami_ids)):
            if not catalog:
                raise ValueError(f"node image catalog has no {name} images")
            for region, ami_id in catalog.items():
                if not _REGION_PATTERN.fullmatch(region):
                    raise ValueError(f"node image catalog has an invalid {name} region")
                if not _AMI_PATTERN.fullmatch(ami_id):
                    raise ValueError(f"node image catalog has an invalid {name} AMI ID")
        if set(self.cpu_ami_ids.values()) & set(self.gpu_ami_ids.values()):
            raise ValueError("CPU and GPU node image catalogs must be distinct")
        return self

    def payload(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()


class _Tag(_AwsModel):
    key: str = Field(alias="Key")
    value: str = Field(alias="Value")


class _Image(_AwsModel):
    image_id: str = Field(alias="ImageId")
    state: str = Field(alias="State")
    tags: list[_Tag] = Field(default_factory=list, alias="Tags")


class _DescribeImagesResponse(_AwsModel):
    images: list[_Image] = Field(alias="Images")


def _parse_ami_ids(raw: str, *, name: str) -> dict[str, str]:
    try:
        value = TypeAdapter(dict[str, str]).validate_json(raw)
    except ValidationError as exc:
        raise SystemExit(f"{name} AMI IDs must map regions to AMI IDs") from exc
    return {region.strip().lower(): ami_id.strip().lower() for region, ami_id in value.items()}


def load_catalog(path: Path) -> NodeImageCatalog:
    try:
        return NodeImageCatalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise SystemExit(f"invalid node image catalog: {path}") from exc


def resolve_catalog(catalog: NodeImageCatalog) -> dict[str, str]:
    expected = host_recipe_sha256()
    if catalog.recipe_sha256 != expected:
        raise SystemExit(
            "the current node image catalog does not match this host recipe; "
            "run the Node Images workflow on this revision before Ship"
        )
    return {
        "node_image_recipe_sha256": catalog.recipe_sha256,
        "cpu_ami_ids": json.dumps(catalog.cpu_ami_ids, sort_keys=True, separators=(",", ":")),
        "gpu_ami_ids": json.dumps(catalog.gpu_ami_ids, sort_keys=True, separators=(",", ":")),
    }


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
        detail = (
            result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        )
        raise SystemExit(f"aws {' '.join(arguments[:2])} failed: {detail[-1000:]}")
    return result


def _verify_images(
    catalogs: Mapping[str, Mapping[str, str]], *, recipe_sha256: str, aws_cli: str
) -> None:
    regions = sorted({region for catalog in catalogs.values() for region in catalog})
    for region in regions:
        expected = {
            ami_id: variant
            for variant, catalog in catalogs.items()
            if (ami_id := catalog.get(region)) is not None
        }
        result = _run_aws(
            aws_cli,
            [
                "ec2",
                "describe-images",
                "--image-ids",
                *sorted(expected),
                "--region",
                region,
                "--output",
                "json",
            ],
        )
        try:
            response = _DescribeImagesResponse.model_validate_json(result.stdout)
        except ValidationError as exc:
            raise SystemExit(f"{region}: AWS returned invalid image metadata") from exc
        if {image.image_id for image in response.images} != set(expected):
            raise SystemExit(f"{region}: not every catalog image was returned by AWS")
        for image in response.images:
            tags = {tag.key: tag.value for tag in image.tags}
            if image.state != "available":
                raise SystemExit(f"{region}: node image {image.image_id} is {image.state}")
            if tags.get(_RECIPE_TAG_KEY) != recipe_sha256:
                raise SystemExit(f"{region}: node image {image.image_id} has the wrong recipe tag")
            if tags.get(_VARIANT_TAG_KEY) != expected[image.image_id]:
                raise SystemExit(f"{region}: node image {image.image_id} has the wrong variant tag")
            if tags.get(_MANAGED_TAG_KEY) != _MANAGED_TAG_VALUE:
                raise SystemExit(f"{region}: node image {image.image_id} is not platform managed")


def _public_url(bucket: str, region: str, key: str) -> str:
    encoded_key = "/".join(pathname2url(part) for part in key.split("/"))
    return f"https://s3.{region}.amazonaws.com/{bucket}/{encoded_key}"


def _put_catalog(
    payload: bytes,
    *,
    bucket: str,
    region: str,
    key: str,
    cache_control: str,
    aws_cli: str,
    immutable: bool,
) -> None:
    checksum = b64encode(sha256(payload).digest()).decode()
    with tempfile.NamedTemporaryFile("wb", suffix=".json") as handle:
        handle.write(payload)
        handle.flush()
        arguments = [
            "s3api",
            "put-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            handle.name,
            "--content-type",
            "application/json",
            "--cache-control",
            cache_control,
            "--checksum-algorithm",
            "SHA256",
            "--checksum-sha256",
            checksum,
            "--region",
            region,
            "--output",
            "json",
        ]
        if immutable:
            arguments.extend(["--if-none-match", "*"])
        result = _run_aws(aws_cli, arguments, check=False)
    if result.returncode != 0:
        detail = f"{result.stdout}\n{result.stderr}"
        if not immutable or "PreconditionFailed" not in detail:
            raise SystemExit(f"could not publish s3://{bucket}/{key}: {detail.strip()[-1000:]}")


def _verify_public_payload(url: str, expected: bytes) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "lazycloud-node-images/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            actual = response.read()
    except (OSError, urllib.error.HTTPError) as exc:
        raise SystemExit(f"node image catalog is not anonymously readable: {url}") from exc
    if actual != expected:
        raise SystemExit(f"published node image catalog has different bytes: {url}")


def publish_catalog(catalog: NodeImageCatalog, *, bucket: str, region: str, aws_cli: str) -> str:
    _verify_images(
        {"cpu": catalog.cpu_ami_ids, "gpu": catalog.gpu_ami_ids},
        recipe_sha256=catalog.recipe_sha256,
        aws_cli=aws_cli,
    )
    payload = catalog.payload()
    digest = sha256(payload).hexdigest()
    immutable_key = f"connected-aws/node-images/catalogs/{digest}.json"
    current_key = "connected-aws/node-images/current.json"
    _put_catalog(
        payload,
        bucket=bucket,
        region=region,
        key=immutable_key,
        cache_control="public, max-age=31536000, immutable",
        aws_cli=aws_cli,
        immutable=True,
    )
    immutable_url = _public_url(bucket, region, immutable_key)
    _verify_public_payload(immutable_url, payload)
    _put_catalog(
        payload,
        bucket=bucket,
        region=region,
        key=current_key,
        cache_control="no-cache",
        aws_cli=aws_cli,
        immutable=False,
    )
    _verify_public_payload(_public_url(bucket, region, current_key), payload)
    return immutable_url


def _write_outputs(outputs: Mapping[str, str], path: Path | None) -> None:
    if path is None:
        for name, value in outputs.items():
            print(f"{name}={value}")
        return
    with path.open("a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            if "\n" in value:
                raise SystemExit(f"catalog output {name} cannot span lines")
            handle.write(f"{name}={value}\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("recipe", help="print the current host recipe digest")

    resolve = subparsers.add_parser("resolve", help="validate a catalog for this revision")
    resolve.add_argument("--catalog", type=Path, required=True)
    resolve.add_argument("--github-output", type=Path)

    publish = subparsers.add_parser("publish", help="publish a verified catalog")
    publish.add_argument("--cpu-ami-ids", required=True)
    publish.add_argument("--gpu-ami-ids", required=True)
    publish.add_argument("--source-revision", required=True)
    publish.add_argument("--bucket", required=True)
    publish.add_argument("--region", required=True)
    publish.add_argument("--aws-cli", default="aws")
    publish.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    if args.command == "recipe":
        print(host_recipe_sha256())
        return 0
    if args.command == "resolve":
        _write_outputs(resolve_catalog(load_catalog(args.catalog)), args.github_output)
        return 0

    if not _S3_NAME_PATTERN.fullmatch(args.bucket):
        raise SystemExit("invalid release bucket name")
    if not _REGION_PATTERN.fullmatch(args.region):
        raise SystemExit("invalid release bucket region")
    try:
        catalog = NodeImageCatalog(
            recipe_sha256=host_recipe_sha256(),
            source_revision=args.source_revision,
            cpu_ami_ids=_parse_ami_ids(args.cpu_ami_ids, name="CPU"),
            gpu_ami_ids=_parse_ami_ids(args.gpu_ami_ids, name="GPU"),
        )
    except ValidationError as exc:
        raise SystemExit("invalid node image catalog inputs") from exc
    url = publish_catalog(catalog, bucket=args.bucket, region=args.region, aws_cli=args.aws_cli)
    if args.output is not None:
        args.output.write_bytes(catalog.payload())
    _write_outputs({"catalog_url": url, "recipe_sha256": catalog.recipe_sha256}, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Publish a complete set of platform images, resuming missing commit artifacts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from pydantic import BaseModel, Field


class Registry(BaseModel):
    registry: str
    repository_prefix: str


class ImageDetail(BaseModel):
    imageDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class Images(BaseModel):
    imageDetails: list[ImageDetail]


class Platform(BaseModel):
    os: str
    architecture: str


class Manifest(BaseModel):
    digest: str
    platform: Platform


class Index(BaseModel):
    manifests: list[Manifest]


class BakeGroup(BaseModel):
    targets: list[str]


class BakeDefinition(BaseModel):
    group: dict[str, BakeGroup]


def published_image(registry: Registry, name: str, commit: str) -> str | None:
    repository = f"{registry.repository_prefix}/{name}"
    result = subprocess.run(
        [
            "aws",
            "ecr",
            "describe-images",
            "--repository-name",
            repository,
            "--image-ids",
            f"imageTag={commit}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        if "ImageNotFoundException" in result.stderr:
            return None
        raise RuntimeError(f"Registry inspection failed for {repository}: {result.stderr}")
    details = Images.model_validate_json(result.stdout).imageDetails
    if len(details) != 1:
        raise RuntimeError(f"Expected one published image for {repository}:{commit}")
    reference = f"{registry.registry}/{repository}@{details[0].imageDigest}"
    index = Index.model_validate_json(
        subprocess.check_output(["docker", "buildx", "imagetools", "inspect", "--raw", reference])
    )
    executable = [
        item
        for item in index.manifests
        if item.platform.os == "linux" and item.platform.architecture == "amd64"
    ]
    if len(executable) != 1:
        raise RuntimeError(f"{name} must contain one linux/amd64 executable")
    reference = f"{registry.registry}/{repository}@{executable[0].digest}"
    subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return reference


def publish(registry: Registry, commit: str) -> dict[str, str]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("A full source commit is required")
    if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != commit:
        raise ValueError("Build tree does not match the selected commit")
    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        raise ValueError("Artifact publication requires a clean source tree")
    definition = BakeDefinition.model_validate_json(
        subprocess.check_output(
            ["docker", "buildx", "bake", "-f", "docker/bake.hcl", "control-plane", "--print"]
        )
    )
    targets = definition.group["control-plane"].targets
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("Control-plane Bake group must contain distinct image targets")
    images: dict[str, str] = {}
    for attempt in range(1, 4):
        missing: list[str] = []
        for name in targets:
            image = published_image(registry, name, commit)
            if image is None:
                missing.append(name)
            else:
                images[name] = image
        if not missing:
            return images
        print(
            json.dumps({"attempt": attempt, "published": list(images), "building": missing}),
            flush=True,
        )
        command: list[str] = [
            "docker",
            "buildx",
            "bake",
            "-f",
            "docker/bake.hcl",
            "--push",
            *missing,
        ]
        for name in missing:
            command += [
                "--set",
                f"{name}.tags={registry.registry}/{registry.repository_prefix}/{name}:{commit}",
                "--set",
                f"{name}.platform=linux/amd64",
                "--set",
                f"{name}.attest=type=provenance,mode=min",
                "--set",
                f"{name}.cache-from=type=gha,scope={name}",
                "--set",
                f"{name}.cache-to=type=gha,scope={name},mode=max",
            ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        if process.stdout is None:
            raise RuntimeError("Build output pipe was not created")
        recent: list[str] = []
        for line in process.stdout:
            print(line, end="", flush=True)
            recent = [*recent[-99:], line]
        code = process.wait()
        if code and not re.search(
            r"upload with id .* does not exist|TLS handshake timeout|unexpected status.*5\d\d"
            r"|connection reset|i/o timeout",
            "".join(recent),
            re.IGNORECASE,
        ):
            raise RuntimeError("Platform image build failed; see build output")
        if code:
            time.sleep(attempt)
    images = {
        name: image
        for name in targets
        if (image := published_image(registry, name, commit)) is not None
    }
    if set(images) != set(targets):
        raise RuntimeError("Platform publication remains incomplete after three attempts")
    return images


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--infrastructure", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registry = Registry.model_validate_json(args.infrastructure.read_bytes())
    args.output.write_text(json.dumps(publish(registry, args.commit), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()

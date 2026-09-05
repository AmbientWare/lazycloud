"""Export a verified host image as non-secret Terraform input."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from deploy.hetzner.bake import host_recipe_sha256
from provider_hetzner.client import HetznerClient, HetznerError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class BakeMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_image_id: int = Field(gt=0)
    location: str
    bake_id: UUID


class Build(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builder_type: str
    custom_data: BakeMetadata


class Manifest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    builds: list[Build] = Field(min_length=1, max_length=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("HCLOUD_TOKEN", "").strip()
    if not token:
        parser.error("HCLOUD_TOKEN must contain the selected project's API token")
    manifest = Manifest.model_validate_json(args.manifest.read_bytes())
    build = manifest.builds[0]
    metadata = build.custom_data
    if build.builder_type != "hcloud" or metadata.recipe_sha256 != host_recipe_sha256(
        metadata.base_image_id
    ):
        raise ValueError("the manifest does not match the current host recipe")
    client = HetznerClient(SecretStr(token))
    selector = f"lazycloud-bake={metadata.bake_id}"
    if tuple(client.servers(selector)):
        raise ValueError("the image build still has a server; finish its cleanup first")
    snapshots = tuple(client.snapshots(selector))
    if len(snapshots) != 1:
        raise ValueError("the project must contain exactly one snapshot from this build")
    snapshot = snapshots[0]
    if (
        snapshot.status != "available"
        or snapshot.architecture != "x86"
        or snapshot.labels.get("lazycloud-release") != metadata.recipe_sha256[:63]
    ):
        raise ValueError("the snapshot is unavailable or does not match its recipe")
    if not 0 < snapshot.disk_size <= 80:
        raise ValueError("the snapshot must fit the smallest supported node's 80-GiB disk")
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(
            {
                "hetzner_node_images": {
                    metadata.location: {
                        "image_id": snapshot.id,
                        "recipe_sha256": metadata.recipe_sha256,
                    }
                }
            },
            handle,
            indent=2,
        )
        handle.write("\n")
    print(f"Verified image {snapshot.id} in {metadata.location}: {args.output}")
    print("Non-secret Terraform input only; no provider resources or secrets were changed.")


if __name__ == "__main__":
    try:
        main()
    except ValidationError:
        raise SystemExit("Invalid host-image manifest.") from None
    except (HetznerError, ValueError) as exc:
        raise SystemExit(str(exc)) from None

"""Build a credential-free Hetzner CPU host snapshot with Packer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from agent.operations import build_agent_install_script

_DIRECTORY = Path(__file__).resolve().parent


def host_recipe_sha256(base_image_id: int) -> str:
    return sha256(
        build_agent_install_script().encode()
        + (_DIRECTORY.parent / "node-images/prepare-ubuntu-host.sh").read_bytes()
        + (_DIRECTORY / "node.pkr.hcl").read_bytes()
        + str(base_image_id).encode()
    ).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-image-id", type=int, required=True)
    parser.add_argument(
        "--location", choices=("ash", "hil", "fsn1", "nbg1", "hel1", "sin"), required=True
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()
    if args.base_image_id <= 0:
        parser.error("base image ID must be positive")
    if args.manifest.exists():
        parser.error("manifest already exists; select a new output path")
    if not args.validate and not os.environ.get("HCLOUD_TOKEN"):
        parser.error("HCLOUD_TOKEN must contain the selected project's API token")
    if os.environ.get("HCLOUD_ENDPOINT"):
        parser.error("HCLOUD_ENDPOINT overrides are not supported")

    runtime = build_agent_install_script()
    recipe = host_recipe_sha256(args.base_image_id)
    bake_id = str(uuid4())
    print(f"Hetzner bake {bake_id}; recipe {recipe}; location {args.location}", flush=True)
    if args.validate:
        print("Checking template syntax only; no provider resources will be created.", flush=True)
    else:
        print(
            "Build creates one paid server and retains one paid snapshot. "
            "Packer cleans its temporary server and SSH key.",
            flush=True,
        )

    with tempfile.TemporaryDirectory(prefix="lazycloud-hetzner-bake-") as directory:
        temporary = Path(directory)
        installer = temporary / "runtime.sh"
        installer.write_text(runtime, encoding="utf-8")
        variables = temporary / "inputs.pkrvars.json"
        variables.write_text(
            json.dumps(
                {
                    "base_image_id": str(args.base_image_id),
                    "location": args.location,
                    "bake_id": bake_id,
                    "recipe_sha256": recipe,
                    "runtime_installer": str(installer),
                    "manifest_path": str(args.manifest.resolve()),
                }
            ),
            encoding="utf-8",
        )
        template = str(_DIRECTORY / "node.pkr.hcl")
        subprocess.run(["packer", "init", template], check=True)
        command = ["packer", "validate" if args.validate else "build"]
        if args.validate:
            command.append("-syntax-only")
        command.extend([f"-var-file={variables}", template])
        subprocess.run(command, check=True)

    if not args.validate:
        print(
            f"Manifest: {args.manifest}. Audit bake-tagged resources and the server's "
            "primary IP before recording cleanup as complete."
        )


if __name__ == "__main__":
    main()

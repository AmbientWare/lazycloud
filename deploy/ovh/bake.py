"""Publish an OVHcloud CPU node image through a disposable Public Cloud instance."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path
from time import sleep
from uuid import UUID, uuid4

from agent.operations import build_agent_install_script
from dotenv import dotenv_values
from provider_ovh.capacity_policy import OVH_CAPACITY_POLICY
from provider_ovh.client import Flavor, Instance, OvhApiCredentials, OvhClient, OvhError
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter

_PREPARE = Path(__file__).resolve().parents[1] / "node-images" / "prepare-ubuntu-host.sh"


class BakeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bake_id: UUID
    credential_ref: str
    project_id: str
    region: str
    base_image_id: UUID
    recipe_sha256: str
    creation_attempted: bool = False
    operation_id: str | None = None
    instance_id: UUID | None = None
    snapshot_attempted: bool = False
    snapshot_operation_id: str | None = None
    snapshot_finished: bool = False
    snapshot_observed: bool = False
    image_id: UUID | None = None
    published: bool = False
    cleaned: bool = False

    @property
    def instance_name(self) -> str:
        return f"lc-bake-{self.bake_id}"

    @property
    def image_name(self) -> str:
        return f"lazycloud-node-{self.recipe_sha256}-{self.bake_id}"


def save(path: Path, state: BakeState) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
        output.write(state.model_dump_json(indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
        temporary = Path(output.name)
    temporary.replace(path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def instance(client: OvhClient, state: BakeState) -> Instance | None:
    if state.instance_id is not None:
        found = client.instance(str(state.instance_id), state.region)
        if found is not None and found.name != state.instance_name:
            raise RuntimeError("OVHcloud bake instance identity changed; cleanup refused")
        return found
    found = [node for node in client.instances(state.region) if node.name == state.instance_name]
    if len(found) > 1:
        raise RuntimeError("OVHcloud returned duplicate bake instances; inspect the manifest")
    if found:
        state.instance_id = UUID(found[0].id)
        return found[0]
    return None


def cleanup(client: OvhClient, path: Path, state: BakeState) -> None:
    if not state.creation_attempted and not state.snapshot_attempted:
        state.cleaned = True
        save(path, state)
        return
    for cycle in range(3):
        node = instance(client, state)
        operation = (
            client.operation(state.operation_id)
            if state.operation_id and state.instance_id is None
            else None
        )
        if node is None and operation is not None and operation.resource_id:
            node = client.instance(operation.resource_id, state.region)
            if node is not None:
                if node.name != state.instance_name:
                    raise RuntimeError("OVHcloud operation names an unrelated instance")
                state.instance_id = UUID(node.id)
        images = [
            image for image in client.snapshots(state.region) if image.name == state.image_name
        ]
        snapshot_operation = (
            client.operation(state.snapshot_operation_id)
            if state.snapshot_operation_id and not state.snapshot_finished
            else None
        )
        if (
            snapshot_operation is not None
            and snapshot_operation.status in {"completed", "canceled", "in-error"}
            and state.snapshot_observed
        ) or any(image.status in {"active", "error"} for image in images):
            state.snapshot_finished = True
        if len(images) > 1:
            raise RuntimeError("OVHcloud returned duplicate bake snapshots; inspect the manifest")
        if images:
            if state.image_id is not None and str(state.image_id) != images[0].id:
                raise RuntimeError("OVHcloud bake snapshot identity changed; cleanup refused")
            state.image_id = UUID(images[0].id)
            state.snapshot_observed = True
        save(path, state)
        print(
            f"Cleanup {cycle + 1}: instance={node.status if node else 'absent'} "
            f"create={operation.status if operation else 'unknown'} snapshots={len(images)} "
            f"snapshot={snapshot_operation.status if snapshot_operation else 'unknown'}",
            flush=True,
        )
        if node is not None:
            if node.attached_volumes or any(
                node.id in volume.attached_to for volume in client.volumes()
            ):
                raise RuntimeError(
                    "Bake instance has unexpected persistent volumes; deletion refused"
                )
            client.delete_instance(node.id)
        if images and not state.published:
            client.delete_snapshot(images[0].id)
        create_settled = (
            not state.creation_attempted
            or state.instance_id is not None
            or (
                operation is not None
                and operation.status in {"canceled", "in-error"}
                and operation.resource_id is None
                and not operation.sub_operations
            )
        )
        snapshot_settled = not state.snapshot_attempted or state.snapshot_finished
        if node is None and create_settled and snapshot_settled and (not images or state.published):
            state.cleaned = True
            save(path, state)
            return
        sleep(3)
    raise RuntimeError(f"OVHcloud cleanup is unresolved; rerun --cleanup with manifest {path}")


def ssh_command(address: str, key: Path, known_hosts: Path) -> list[str]:
    return [
        "ssh",
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        f"root@{address}",
    ]


def build(client: OvhClient, path: Path, state: BakeState, runtime: str) -> None:
    image = client.image(str(state.base_image_id))
    if image.region != state.region or image.status != "active" or "Ubuntu 24.04" not in image.name:
        raise RuntimeError("Select an active Ubuntu 24.04 image in the requested region")
    catalog = client.catalog()
    disk_price = catalog.plan("instance.local-storage-gen3-gb.hour.consumption")
    ip_price = catalog.plan("publicip.ip.hour.consumption").hourly_micros()
    offers: list[tuple[int, Flavor]] = []
    for shape in client.flavors(state.region):
        if (
            not shape.available
            or shape.quota <= 0
            or shape.os_type != "linux"
            or shape.name not in OVH_CAPACITY_POLICY.allowed_instance_types
            or shape.disk < max(OVH_CAPACITY_POLICY.root_volume_gib, image.min_disk)
            or shape.ram * 1024 < image.min_ram
            or not shape.plan_codes.hourly
        ):
            continue
        cost = (
            catalog.plan(shape.plan_codes.hourly).hourly_micros()
            + disk_price.hourly_micros(quantity=shape.disk)
            + ip_price
        )
        offers.append((cost, shape))
    if not offers:
        raise RuntimeError("No approved OVHcloud builder flavor has stock and quota")
    _, flavor = min(offers, key=lambda offer: offer[0])
    with tempfile.TemporaryDirectory(prefix="lazycloud-ovh-bake-") as directory:
        temporary = Path(directory)
        key = temporary / "builder"
        host_key = temporary / "host"
        for key_path in (key, host_key):
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)], check=True
            )
        public_key = key.with_suffix(".pub").read_text().strip()
        host_public_key = host_key.with_suffix(".pub").read_text().strip()
        cloud_config: dict[str, JsonValue] = {
            "disable_root": False,
            "ssh_pwauth": False,
            "users": [{"name": "root", "lock_passwd": True, "ssh_authorized_keys": [public_key]}],
            "ssh_keys": {
                "ed25519_private": host_key.read_text(),
                "ed25519_public": host_public_key,
            },
        }
        if instance(client, state) is not None:
            raise RuntimeError("A bake instance already exists for this manifest")
        state.creation_attempted = True
        save(path, state)
        try:
            operation = client.create_instance(
                state.region,
                {
                    "name": state.instance_name,
                    "flavor": {"id": flavor.id},
                    "billingPeriod": "hourly",
                    "bootFrom": {"imageId": str(state.base_image_id)},
                    "network": {"public": True},
                    "bulk": 1,
                    "userData": "#cloud-config\n" + json.dumps(cloud_config),
                },
            )
        except OvhError as exc:
            if exc.status_code in {400, 401, 403, 404, 422, 429}:
                state.creation_attempted = False
                save(path, state)
            raise
        state.operation_id = operation.id
        save(path, state)
        command: list[str] | None = None
        for cycle in range(120):
            operation = client.operation(state.operation_id)
            node = instance(client, state)
            save(path, state)
            address = (
                next(
                    (ip.ip for ip in node.ip_addresses if ip.version == 4 and ip.type == "public"),
                    "",
                )
                if node
                else ""
            )
            print(
                f"Boot {cycle + 1}: operation={operation.status} "
                f"instance={node.status if node else 'absent'} ipv4={address or 'absent'}",
                flush=True,
            )
            if operation.status in {"canceled", "in-error"} or (node and node.status == "ERROR"):
                raise RuntimeError("OVHcloud builder creation failed")
            if address:
                known_hosts = temporary / "known_hosts"
                known_hosts.write_text(f"{address} {host_public_key}\n")
                candidate = ssh_command(address, key, known_hosts)
                probe = subprocess.run(
                    [
                        *candidate,
                        "cloud-init status; test -f /var/lib/cloud/instance/boot-finished",
                    ],
                    check=False,
                    timeout=8,
                )
                if probe.returncode == 0:
                    command = candidate
                    break
            sleep(3)
        if command is None:
            raise RuntimeError("OVHcloud builder did not finish cloud-init")
        installer = "set -Eeuo pipefail\n(\n" + runtime + "\n)\n" + _PREPARE.read_text()
        process = subprocess.Popen([*command, "bash -s -- --runtime-only"], stdin=subprocess.PIPE)
        if process.stdin is None:
            raise RuntimeError("OVHcloud builder has no script input")
        try:
            process.stdin.write(installer.encode())
            process.stdin.close()
            for cycle in range(360):
                result = process.poll()
                node = instance(client, state)
                print(
                    f"Install {cycle + 1}: instance={node.status if node else 'absent'} "
                    f"exit={result}",
                    flush=True,
                )
                if result is not None:
                    if result != 0:
                        raise RuntimeError("OVHcloud node runtime installation failed")
                    break
                sleep(3)
            else:
                raise RuntimeError("OVHcloud node runtime installation exceeded its build budget")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        if state.instance_id is None:
            raise RuntimeError("OVHcloud builder has no recorded instance identity")
        state.snapshot_attempted = True
        save(path, state)
        try:
            snapshot = client.create_snapshot(
                state.region, str(state.instance_id), state.image_name
            )
        except OvhError as exc:
            if exc.status_code in {400, 401, 403, 404, 422, 429}:
                state.snapshot_attempted = False
                save(path, state)
            raise
        state.image_id = UUID(snapshot.image_id)
        state.snapshot_operation_id = snapshot.operation_id
        save(path, state)
        for cycle in range(120):
            operation = client.operation(snapshot.operation_id)
            images = [
                item for item in client.snapshots(state.region) if item.id == snapshot.image_id
            ]
            if images:
                state.snapshot_observed = True
                save(path, state)
            print(
                f"Snapshot {cycle + 1}: operation={operation.status} "
                f"image={images[0].status if images else 'absent'}",
                flush=True,
            )
            if operation.status in {"canceled", "in-error"}:
                raise RuntimeError("OVHcloud node snapshot failed")
            if (
                len(images) == 1
                and images[0].name == state.image_name
                and images[0].status == "active"
            ):
                state.published = True
                state.snapshot_finished = True
                save(path, state)
                return
            sleep(3)
        raise RuntimeError("OVHcloud snapshot did not become active")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--credential-ref", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--region", choices=OVH_CAPACITY_POLICY.allowed_regions)
    parser.add_argument("--base-image-id", type=UUID)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.manifest.with_suffix(args.manifest.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another process owns this bake manifest")
        run(parser, args)


def run(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    deployment = dotenv_values(args.env_file)
    credentials_json = deployment.get("LAZYCLOUD_PLATFORM_CAPACITY_OVH_CREDENTIALS")
    if not credentials_json:
        parser.error("credential file must set LAZYCLOUD_PLATFORM_CAPACITY_OVH_CREDENTIALS")
    credentials = TypeAdapter(
        dict[str, OvhApiCredentials],
        config=ConfigDict(hide_input_in_errors=True),
    ).validate_json(credentials_json)[args.credential_ref]
    runtime = build_agent_install_script()
    project_id = args.project_id or deployment.get("OVH_PROJECT_ID")
    if args.cleanup:
        state = BakeState.model_validate_json(args.manifest.read_bytes())
        if state.credential_ref != args.credential_ref:
            parser.error("manifest belongs to another credential binding")
    else:
        if args.manifest.exists() or not project_id or not args.region or not args.base_image_id:
            parser.error("a new bake requires project, region, base image, and a new manifest path")
        recipe = sha256(
            runtime.encode()
            + _PREPARE.read_bytes()
            + Path(__file__).read_bytes()
            + str(args.base_image_id).encode()
        ).hexdigest()
        state = BakeState(
            bake_id=uuid4(),
            credential_ref=args.credential_ref,
            project_id=project_id,
            region=args.region,
            base_image_id=args.base_image_id,
            recipe_sha256=recipe,
        )
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        save(args.manifest, state)
    client = OvhClient(
        credentials.application_key,
        credentials.application_secret,
        credentials.consumer_key,
        state.project_id,
    )
    print(
        f"OVHcloud bake {state.bake_id}; region {state.region}; manifest {args.manifest}",
        flush=True,
    )
    if args.cleanup:
        cleanup(client, args.manifest, state)
        return
    print("Creating a paid temporary instance and retaining one paid node image.", flush=True)
    try:
        build(client, args.manifest, state, runtime)
    finally:
        cleanup(client, args.manifest, state)
    print(
        json.dumps(
            {state.region: {"image_id": str(state.image_id), "recipe_sha256": state.recipe_sha256}}
        )
    )


if __name__ == "__main__":
    main()

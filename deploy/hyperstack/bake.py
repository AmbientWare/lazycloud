"""Build and publish a Hyperstack GPU host image with scoped resource cleanup."""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import shlex
import subprocess
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from agent.operations import build_agent_install_script
from dotenv import load_dotenv
from provider_hyperstack.client import (
    HyperstackClient,
    HyperstackError,
    Image,
    Keypair,
    Server,
    Snapshot,
)
from provider_hyperstack.identity import labels
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, TypeAdapter

_DIRECTORY = Path(__file__).resolve().parent
_REJECTED_CREATION_CODES = {400, 401, 403, 404, 405, 422}


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bake_id: UUID
    provider_ref: str
    region: str = "US-1"
    environment_name: str
    base_image_id: int = Field(gt=0)
    base_image_name: str = ""
    ssh_source_cidr: str
    recipe_sha256: str
    flavor: str = "n3-A100-SXM4x8"
    key_attempted: bool = False
    vm_attempted: bool = False
    snapshot_attempted: bool = False
    image_attempted: bool = False
    keypair_id: int | None = None
    keypair_fingerprint: str | None = None
    server_id: int | None = None
    snapshot_id: int | None = None
    image_id: int | None = None
    prepared: bool = False
    published: bool = False
    cleanup_complete: bool = False

    @property
    def name(self) -> str:
        return f"lc-bake-{self.bake_id}"

    @property
    def image_name(self) -> str:
        return f"lazycloud-node-{self.recipe_sha256[:16]}-{self.bake_id}"

    @property
    def ownership_labels(self) -> tuple[str, ...]:
        return (f"lazycloud-bake={self.bake_id}", f"lazycloud-release={self.recipe_sha256}")


def _script() -> str:
    runtime = build_agent_install_script()
    gpu = (_DIRECTORY / "prepare-gpu.sh").read_text()
    finalize = (_DIRECTORY.parent / "node-images/prepare-ubuntu-host.sh").read_text()
    version = (_DIRECTORY.parent / "ami/gvisor-version").read_text().strip()
    return (
        "#!/bin/bash\nset -Eeuo pipefail\n"
        f"GVISOR_VERSION={shlex.quote(version)}\nexport GVISOR_VERSION\n"
        "runtime_installer=$(mktemp)\ncat > \"$runtime_installer\" <<'LC_RUNTIME_INSTALLER'\n"
        + runtime
        + '\nLC_RUNTIME_INSTALLER\nsh "$runtime_installer" --runtime-only\n'
        + 'rm "$runtime_installer"\n'
        + gpu
        + "\n"
        + finalize
        + "\n"
    )


def _recipe(base_image_id: int) -> str:
    return sha256(str(base_image_id).encode() + _script().encode()).hexdigest()


def _save(path: Path, manifest: Manifest) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        os.chmod(temporary, 0o600)
        output.write(manifest.model_dump_json(indent=2) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _key_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".ssh")


def _keypair(client: HyperstackClient, manifest: Manifest) -> Keypair | None:
    matches = [key for key in client.keypairs() if key.name == manifest.name]
    if len(matches) > 1:
        raise RuntimeError("build key name is ambiguous")
    if not matches:
        return None
    key = matches[0]
    if (
        key.environment.name != manifest.environment_name
        or key.environment.region != manifest.region
        or sha256(" ".join(key.public_key.split()[:2]).encode()).hexdigest()
        != manifest.keypair_fingerprint
        or manifest.keypair_id not in {None, key.id}
    ):
        raise RuntimeError("provider key does not belong to this image build")
    return key


def _server(client: HyperstackClient, manifest: Manifest) -> Server | None:
    server = (
        client.server_by_id(manifest.server_id)
        if manifest.server_id
        else client.server(manifest.name)
    )
    if server is None:
        return None
    metadata = labels(server.labels)
    if (
        server.name != manifest.name
        or metadata.get("lazycloud-bake") != str(manifest.bake_id)
        or metadata.get("lazycloud-release") != manifest.recipe_sha256
        or server.environment.name != manifest.environment_name
        or server.environment.region != manifest.region
        or server.flavor.name != manifest.flavor
        or server.volume_attachments
    ):
        raise RuntimeError("provider VM does not belong to this image build")
    return server


def _snapshot(client: HyperstackClient, manifest: Manifest) -> Snapshot | None:
    if manifest.snapshot_id is not None:
        snapshot = client.snapshot(manifest.snapshot_id)
    else:
        matches = [
            item for item in client.snapshots(search=manifest.name) if item.name == manifest.name
        ]
        if len(matches) > 1:
            raise RuntimeError("build snapshot name is ambiguous")
        snapshot = matches[0] if matches else None
    if snapshot is not None and (
        snapshot.name != manifest.name or snapshot.vm_id != manifest.server_id
    ):
        raise RuntimeError("provider snapshot does not belong to this image build")
    return snapshot


def _image(client: HyperstackClient, manifest: Manifest) -> Image | None:
    if manifest.image_id is not None:
        image = client.image(manifest.image_id)
    else:
        matches = [
            item
            for item in client.images(region=manifest.region, search=manifest.image_name)
            if item.name == manifest.image_name
        ]
        if len(matches) > 1:
            raise RuntimeError("published image name is ambiguous")
        image = matches[0] if matches else None
    if image is not None and (
        image.name != manifest.image_name or image.region_name != manifest.region or image.is_public
    ):
        raise RuntimeError("provider image does not match the image build")
    return image


def _ssh(path: Path, server: Server, command: str) -> list[str]:
    if not server.floating_ip:
        raise RuntimeError("build server has no public IP")
    ipaddress.IPv4Address(server.floating_ip)
    host_public_key = Path(f"{path}.host.pub").read_text().strip()
    Path(f"{path}.known_hosts").write_text(f"{server.floating_ip} {host_public_key}\n")
    return [
        "ssh",
        "-i",
        str(_key_path(path)),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={path}.known_hosts",
        f"ubuntu@{server.floating_ip}",
        command,
    ]


def _prepare(client: HyperstackClient, path: Path, manifest: Manifest) -> None:
    for cycle in range(120):
        server = _server(client, manifest)
        if server is None:
            raise RuntimeError("build server disappeared")
        print(
            json.dumps(
                {
                    "cycle": cycle,
                    "vm": server.id,
                    "status": server.status,
                    "public_ip": server.floating_ip,
                    "phase": "ssh-bootstrap",
                }
            ),
            flush=True,
        )
        if server.status in {"ERROR", "SHUTOFF", "HIBERNATED"}:
            raise RuntimeError("build server cannot start host preparation")
        if server.status == "ACTIVE" and server.floating_ip:
            probe = subprocess.run(
                _ssh(
                    path,
                    server,
                    "cloud-init status; sudo tail -n 5 /var/log/cloud-init-output.log; "
                    "test -f /var/lib/cloud/instance/boot-finished",
                ),
                check=False,
                timeout=10,
            )
            if probe.returncode == 0:
                with tempfile.TemporaryFile() as script:
                    script.write(_script().encode())
                    script.seek(0)
                    with subprocess.Popen(
                        _ssh(path, server, "sudo bash -s"), stdin=script
                    ) as process:
                        try:
                            for install_cycle in range(600):
                                result = process.poll()
                                current = _server(client, manifest)
                                print(
                                    json.dumps(
                                        {
                                            "cycle": install_cycle,
                                            "phase": "install",
                                            "vm": current.status if current else None,
                                            "exit": result,
                                        }
                                    ),
                                    flush=True,
                                )
                                if result is not None:
                                    if result != 0:
                                        raise RuntimeError("GPU host preparation failed")
                                    break
                                time.sleep(3)
                            else:
                                raise RuntimeError("GPU host preparation exceeded its build budget")
                        finally:
                            if process.poll() is None:
                                process.kill()
                            process.wait(timeout=5)
                manifest.prepared = True
                _save(path, manifest)
                return
        time.sleep(3)
    raise RuntimeError(
        "build VM did not finish bootstrap; inspect its printed cloud-init diagnostics"
    )


def _cleanup(client: HyperstackClient, path: Path, manifest: Manifest) -> None:
    server = _server(client, manifest)
    if server is not None:
        manifest.server_id = server.id
        _save(path, manifest)
        client.delete_server(server.id)
    if manifest.vm_attempted and manifest.server_id is None:
        raise RuntimeError(
            "VM create outcome remains unknown; resume cleanup after provider reconciliation"
        )
    for cycle in range(60):
        server = _server(client, manifest)
        key = _keypair(client, manifest)
        snapshot = _snapshot(client, manifest) if manifest.snapshot_attempted else None
        image = _image(client, manifest) if manifest.image_attempted else None
        if snapshot is not None:
            manifest.snapshot_id = snapshot.id
        if image is not None:
            manifest.image_id = image.id
        _save(path, manifest)
        print(
            json.dumps(
                {
                    "cycle": cycle,
                    "phase": "cleanup",
                    "vm_present": server is not None,
                    "key_present": key is not None,
                    "snapshot_present": snapshot is not None,
                    "image_present": image is not None,
                }
            ),
            flush=True,
        )
        if not manifest.published:
            if image is not None:
                client.delete_image(image.id)
            if snapshot is not None:
                client.delete_snapshot(snapshot.id)
        if server is None:
            if key is not None and not manifest.published:
                manifest.keypair_id = key.id
                _save(path, manifest)
                client.delete_keypair(key.id)
            if not manifest.published and _keypair(client, manifest) is not None:
                continue
            if manifest.key_attempted and manifest.keypair_id is None:
                raise RuntimeError(
                    "SSH key create outcome remains unknown; keep this build manifest"
                )
            if not manifest.published:
                if manifest.snapshot_attempted and manifest.snapshot_id is None:
                    raise RuntimeError("Snapshot create outcome remains unknown; rerun --cleanup")
                if manifest.image_attempted and manifest.image_id is None:
                    raise RuntimeError("Image create outcome remains unknown; rerun --cleanup")
                if snapshot is not None or image is not None:
                    time.sleep(3)
                    continue
            manifest.cleanup_complete = True
            _save(path, manifest)
            for owned in (
                _key_path(path),
                Path(str(_key_path(path)) + ".pub"),
                Path(f"{path}.known_hosts"),
                Path(f"{path}.host"),
                Path(f"{path}.host.pub"),
            ):
                owned.unlink(missing_ok=True)
            return
        time.sleep(3)
    raise RuntimeError("build VM deletion is incomplete; rerun --cleanup with this manifest")


def _build(client: HyperstackClient, path: Path, manifest: Manifest) -> None:
    if manifest.published:
        if _image(client, manifest) is None:
            raise RuntimeError("published image no longer exists")
        return
    if manifest.cleanup_complete:
        raise RuntimeError("this unfinished build was cleaned up; use a new manifest")
    if not any(
        env.name == manifest.environment_name and env.region == manifest.region
        for env in client.environments()
    ):
        raise RuntimeError("build environment does not exist in US-1")
    base = next(
        (
            image
            for image in client.images(region=manifest.region, search="", include_public=True)
            if image.id == manifest.base_image_id
        ),
        None,
    )
    if base is None or "Ubuntu Server 24.04 LTS" not in base.name or "CUDA" in base.name:
        raise RuntimeError("base image must be a plain Ubuntu24.04 image in US-1")
    manifest.base_image_name = base.name
    _save(path, manifest)
    key = _keypair(client, manifest)
    if key is None:
        if manifest.key_attempted:
            raise RuntimeError("SSH key creation remains unresolved; no duplicate request was sent")
        if _key_path(path).exists():
            raise RuntimeError("build SSH key path already exists")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(_key_path(path))], check=True
        )
        manifest.key_attempted = True
        manifest.keypair_fingerprint = sha256(
            " ".join(Path(str(_key_path(path)) + ".pub").read_text().split()[:2]).encode()
        ).hexdigest()
        _save(path, manifest)
        try:
            key = client.import_keypair(
                name=manifest.name,
                environment_name=manifest.environment_name,
                public_key=Path(str(_key_path(path)) + ".pub").read_text(),
            )
        except HyperstackError as exc:
            if exc.status_code in _REJECTED_CREATION_CODES:
                manifest.key_attempted = False
                _save(path, manifest)
            raise
    manifest.keypair_id = key.id
    _save(path, manifest)
    server = _server(client, manifest)
    if server is None:
        if manifest.vm_attempted:
            raise RuntimeError("VM creation remains unresolved; no duplicate purchase was sent")
        host_key = Path(f"{path}.host")
        if host_key.exists():
            raise RuntimeError("build SSH host key path already exists")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True
        )
        manifest.vm_attempted = True
        _save(path, manifest)
        body: dict[str, JsonValue] = {
            "name": manifest.name,
            "environment_name": manifest.environment_name,
            "flavor_name": manifest.flavor,
            "image_name": base.name,
            "key_name": key.name,
            "count": 1,
            "assign_floating_ip": True,
            "enable_port_randomization": False,
            "create_bootable_volume": False,
            "labels": list(manifest.ownership_labels),
            "user_data": "#cloud-config\n"
            + json.dumps(
                {
                    "ssh_pwauth": False,
                    "ssh_keys": {
                        "ed25519_private": host_key.read_text(),
                        "ed25519_public": Path(f"{path}.host.pub").read_text(),
                    },
                }
            ),
            "security_rules": [
                {
                    "direction": "ingress",
                    "protocol": "tcp",
                    "ethertype": "IPv4",
                    "remote_ip_prefix": manifest.ssh_source_cidr,
                    "port_range_min": 22,
                    "port_range_max": 22,
                }
            ],
        }
        try:
            server = client.create_server(body)
        except HyperstackError as exc:
            if exc.status_code in _REJECTED_CREATION_CODES:
                manifest.vm_attempted = False
                _save(path, manifest)
            raise
    manifest.server_id = server.id
    _save(path, manifest)
    if not manifest.prepared:
        _prepare(client, path, manifest)
    snapshot = _snapshot(client, manifest)
    if snapshot is None:
        if manifest.snapshot_attempted:
            raise RuntimeError(
                "snapshot creation remains unresolved; no duplicate request was sent"
            )
        manifest.snapshot_attempted = True
        _save(path, manifest)
        try:
            snapshot = client.create_snapshot(
                server.id, name=manifest.name, labels=manifest.ownership_labels
            )
        except HyperstackError as exc:
            if exc.status_code in _REJECTED_CREATION_CODES:
                manifest.snapshot_attempted = False
                _save(path, manifest)
            raise
    manifest.snapshot_id = snapshot.id
    _save(path, manifest)
    for cycle in range(240):
        snapshot = _snapshot(client, manifest)
        server = _server(client, manifest)
        print(
            json.dumps(
                {
                    "cycle": cycle,
                    "phase": "snapshot",
                    "snapshot": snapshot.status if snapshot else None,
                    "vm": server.status if server else None,
                }
            ),
            flush=True,
        )
        if snapshot is None or snapshot.status in {"error", "failed"}:
            raise RuntimeError("image snapshot failed")
        if snapshot.status == "success":
            break
        time.sleep(3)
    else:
        raise RuntimeError("snapshot did not complete; its ID remains in the build manifest")
    image = _image(client, manifest)
    if image is None:
        if manifest.image_attempted:
            raise RuntimeError(
                "image publication remains unresolved; no duplicate request was sent"
            )
        manifest.image_attempted = True
        _save(path, manifest)
        try:
            manifest.image_id = client.create_image(
                snapshot.id, name=manifest.image_name, labels=manifest.ownership_labels
            )
        except HyperstackError as exc:
            if exc.status_code in _REJECTED_CREATION_CODES:
                manifest.image_attempted = False
                _save(path, manifest)
            raise
        _save(path, manifest)
    else:
        manifest.image_id = image.id
    for cycle in range(60):
        image = _image(client, manifest)
        snapshot = _snapshot(client, manifest)
        print(
            json.dumps(
                {
                    "cycle": cycle,
                    "phase": "publication",
                    "image_present": image is not None,
                    "snapshot": snapshot.status if snapshot else None,
                }
            ),
            flush=True,
        )
        if image is not None and snapshot is not None and snapshot.is_image:
            manifest.published = True
            _save(path, manifest)
            return
        time.sleep(3)
    raise RuntimeError("image publication is incomplete; resume with this manifest")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--provider-ref", default="hyperstack:us")
    parser.add_argument("--base-image-id", type=int, default=862)
    parser.add_argument("--environment-name", default="default-US-1")
    parser.add_argument("--ssh-source-cidr")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.validate:
        subprocess.run(["bash", "-n"], input=_script(), text=True, check=True)
        print(
            f"Host script syntax passed; recipe {_recipe(args.base_image_id)}. "
            "No resources created."
        )
        return
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    path = args.manifest.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(f"{path}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            manifest = Manifest.model_validate_json(path.read_text())
            if not args.cleanup and manifest.recipe_sha256 != _recipe(manifest.base_image_id):
                raise RuntimeError(
                    "image recipe changed; clean up this build and use a new manifest"
                )
        else:
            if args.cleanup or not args.ssh_source_cidr:
                parser.error(
                    "new builds require --ssh-source-cidr; cleanup requires an existing manifest"
                )
            network = ipaddress.IPv4Network(args.ssh_source_cidr, strict=True)
            if network.prefixlen != 32:
                parser.error("--ssh-source-cidr must be the build operator's single IPv4 /32")
            manifest = Manifest(
                bake_id=uuid4(),
                provider_ref=args.provider_ref,
                environment_name=args.environment_name,
                base_image_id=args.base_image_id,
                ssh_source_cidr=str(network),
                recipe_sha256=_recipe(args.base_image_id),
            )
            _save(path, manifest)
        tokens = TypeAdapter(
            dict[str, SecretStr], config=ConfigDict(hide_input_in_errors=True)
        ).validate_json(os.environ["LAZYCLOUD_PLATFORM_CAPACITY_HYPERSTACK_TOKENS"])
        client = HyperstackClient(tokens[manifest.provider_ref])
        print(
            f"Build {manifest.bake_id}; creates one paid GPU VM "
            "and retains its published snapshot/image.",
            flush=True,
        )
        try:
            if not args.cleanup:
                _build(client, path, manifest)
        finally:
            _cleanup(client, path, manifest)
        print(
            f"Manifest: {path}; published={manifest.published}; "
            f"cleanup={manifest.cleanup_complete}",
            flush=True,
        )
        if manifest.published:
            print(
                json.dumps(
                    {
                        manifest.region: {
                            "environment_name": manifest.environment_name,
                            "keypair_name": manifest.name,
                            "image_name": manifest.image_name,
                            "recipe_sha256": manifest.recipe_sha256,
                        }
                    }
                )
            )


if __name__ == "__main__":
    main()

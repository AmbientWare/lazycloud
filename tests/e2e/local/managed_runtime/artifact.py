"""Verify one selected managed-runtime artifact from an existing worker image."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BLOCKED = 77


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", choices=("3.10", "3.11", "3.12"), required=True)
    parser.add_argument("--image", default=os.getenv("LAZYCLOUD_MANAGED_RUNTIME_IMAGE", ""))
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        return _blocked("managed-runtime artifact acceptance requires --live")
    if shutil.which("docker") is None:
        return _blocked("Docker is required")
    if not args.image:
        return _blocked("--image or LAZYCLOUD_MANAGED_RUNTIME_IMAGE is required")
    if _run(["docker", "image", "inspect", args.image]).returncode != 0:
        return _blocked(f"prepared worker image is unavailable: {args.image}")
    if _run(["docker", "image", "inspect", f"python:{args.python}-slim"]).returncode != 0:
        return _blocked(f"prepared Python image is unavailable: python:{args.python}-slim")

    container_id = ""
    with tempfile.TemporaryDirectory(prefix="lazycloud-managed-runtime-") as temporary:
        root = Path(temporary)
        try:
            created = _run(["docker", "create", args.image], capture=True)
            if created.returncode != 0:
                raise RuntimeError(created.stderr.strip() or "worker container creation failed")
            container_id = created.stdout.strip()
            copied = _run(
                [
                    "docker",
                    "cp",
                    f"{container_id}:/opt/lazycloud/managed-runtime-artifacts/.",
                    str(root),
                ],
                capture=True,
            )
            if copied.returncode != 0:
                raise RuntimeError(copied.stderr.strip() or "artifact extraction failed")
        finally:
            if container_id:
                removed = _run(["docker", "rm", "-f", container_id], capture=True)
                if removed.returncode != 0:
                    raise RuntimeError(
                        removed.stderr.strip() or "temporary worker container cleanup failed"
                    )

        catalog = json.loads((root / "catalog.json").read_text())
        artifact = catalog["artifacts"][args.python]
        completed = _run(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                f"type=bind,source={root},target=/opt/lazycloud/managed-runtime,readonly",
                "--env",
                f"LAZYCLOUD_MANAGED_RUNTIME_CATALOG_DIGEST={catalog['digest']}",
                "--env",
                f"LAZYCLOUD_MANAGED_RUNTIME_DIGEST={artifact['digest']}",
                f"python:{args.python}-slim",
                "sh",
                "-ceu",
                (
                    'python -m pip install --quiet "pydantic==2.10.6" && '
                    "PYTHONDONTWRITEBYTECODE=1 python "
                    "/opt/lazycloud/managed-runtime/launcher.py --verify"
                ),
            ],
            capture=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "managed runtime verification failed")
        evidence = json.loads(completed.stdout)
        if evidence.get("python") != args.python:
            raise RuntimeError("managed runtime selected the wrong Python artifact")
        if evidence.get("resolved_distributions", {}).get("pydantic") != "2.10.6":
            raise RuntimeError("managed runtime did not honor a compatible user dependency")

    print(
        json.dumps(
            {
                "accepted": True,
                "image": args.image,
                "python": args.python,
                "artifact_digest": evidence["artifact_digest"],
                "cleanup": "temporary container and extracted files removed",
            },
            sort_keys=True,
        )
    )
    return 0


def _run(command: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        text=True,
        capture_output=capture,
        stdout=subprocess.DEVNULL if not capture else None,
        stderr=subprocess.DEVNULL if not capture else None,
        check=False,
    )


def _blocked(reason: str) -> int:
    print(f"blocked: {reason}", file=sys.stderr)
    return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())

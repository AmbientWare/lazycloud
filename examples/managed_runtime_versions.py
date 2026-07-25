from __future__ import annotations

import importlib.metadata
import os
import sys
import time

from lazycloud.json_contracts import parse_json_object
from typing_extensions import TypedDict

from lazycloud import App, Client, Image, PythonVersion

runtime_app = App("managed_runtime_versions")
USER_PYDANTIC_VERSION = "2.10.6"


class RuntimeProbeResult(TypedDict):
    value: int
    python: str
    pydantic: str
    artifact_digest: str


def _image(version: PythonVersion) -> Image:
    return Image(
        python_version=version,
        python_packages=[f"pydantic=={USER_PYDANTIC_VERSION}"],
    )


def _probe(value: int) -> RuntimeProbeResult:
    return {
        "value": value,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "pydantic": importlib.metadata.version("pydantic"),
        "artifact_digest": os.environ.get("LAZYCLOUD_MANAGED_RUNTIME_DIGEST", ""),
    }


@runtime_app.function(name="managed-runtime-py310", image=_image(PythonVersion.Py310))
def probe_python310(value: int) -> RuntimeProbeResult:
    return _probe(value)


@runtime_app.function(name="managed-runtime-py311", image=_image(PythonVersion.Py311))
def probe_python311(value: int) -> RuntimeProbeResult:
    return _probe(value)


@runtime_app.function(name="managed-runtime-py312", image=_image(PythonVersion.Py312))
def probe_python312(value: int) -> RuntimeProbeResult:
    return _probe(value)


def run_managed_runtime_version_matrix(value: int = 17) -> dict[str, RuntimeProbeResult]:
    targets = [
        ("3.10", probe_python310),
        ("3.11", probe_python311),
        ("3.12", probe_python312),
    ]
    deployment_ids: list[str] = []
    results: dict[str, RuntimeProbeResult] = {}
    try:
        for version, target in targets:
            deployment = target.deploy(name=f"managed-runtime-{version}-{int(time.time())}")
            deployment_id = parse_json_object(deployment.model_dump_json()).get("deployment_id")
            if not isinstance(deployment_id, str) or not deployment_id:
                raise RuntimeError("managed runtime function deployment returned a cron response")
            deployment_ids.append(deployment_id)
            result = target.remote(value)
            if result["python"] != version:
                raise RuntimeError(f"Python {version} workload ran under {result['python']}")
            if result["pydantic"] != USER_PYDANTIC_VERSION:
                raise RuntimeError(
                    f"Python {version} user dependency was replaced: {result['pydantic']}"
                )
            if len(result["artifact_digest"]) != 64:
                raise RuntimeError(f"Python {version} runtime artifact was not verified")
            results[version] = result
    finally:
        deployments = Client().deployment
        for deployment_id in deployment_ids:
            deployments.delete(deployment_id)
    return results


__all__ = [
    "probe_python310",
    "probe_python311",
    "probe_python312",
    "run_managed_runtime_version_matrix",
]

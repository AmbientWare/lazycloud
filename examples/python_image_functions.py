from __future__ import annotations

import subprocess
import sys

from lazycloud.abstractions.function import Function

from lazycloud import App, ComputePlacementTarget, Image


def _python312_base() -> str:
    return f"python:{sys.version_info.major}.{sys.version_info.minor}"


def _python310_numpy() -> str:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import numpy; assert numpy.array([1, 1, 2, 3]).sum() == 7",
        ],
        check=True,
    )
    return "numpy:7"


def _python311_httpx() -> str:
    import httpx

    return f"httpx:{httpx.__version__}"


def _python312_packaging() -> str:
    import packaging.version

    return f"packaging:{packaging.version.Version('24.2')}"


def python_image_functions(app_slug: str) -> dict[str, Function[[], str]]:
    app = App(app_slug)
    return {
        "python312-base": app.function(
            _python312_base,
            name="python312-base",
            image=Image(python_version="3.12"),
            placement=ComputePlacementTarget.Managed,
        ),
        "python310-numpy": app.function(
            _python310_numpy,
            name="python310-numpy",
            image=Image(python_version="python3.10").add_python_packages(["numpy==1.26.4"]),
            placement=ComputePlacementTarget.Managed,
        ),
        "python311-httpx": app.function(
            _python311_httpx,
            name="python311-httpx",
            image=Image(python_version="python3.11").add_python_packages(["httpx==0.28.1"]),
            placement=ComputePlacementTarget.Managed,
        ),
        "python312-packaging": app.function(
            _python312_packaging,
            name="python312-packaging",
            image=Image(python_version="python3.12").add_python_packages(["packaging==24.2"]),
            placement=ComputePlacementTarget.Managed,
        ),
    }


__all__ = ["python_image_functions"]

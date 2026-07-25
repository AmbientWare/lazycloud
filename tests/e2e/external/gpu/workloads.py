"""GPU image workloads for the opt-in image-build acceptance.

This module is re-imported inside the paid GPU container to resolve each
handler, so it imports only the public SDK and the standard library. Driver
concerns (argument parsing, public clients, cleanup) live in ``image_build``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from uuid import uuid4

from lazycloud import App, Image

APP_NAME = f"e2e_image_gpu_{uuid4().hex[:10]}"
CASE_TIMEOUT_SECONDS = 900
app = App(APP_NAME)


@app.function(
    name="cuda-runtime",
    image=Image.from_registry("nvidia/cuda:12.2.2-cudnn8-runtime-ubuntu22.04").add_python_version(
        "3.10"
    ),
    gpu="L4",
    gpu_count=1,
    timeout_seconds=CASE_TIMEOUT_SECONDS,
)
def cuda_runtime() -> str:
    return subprocess.run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@app.function(
    name="cuda-torch",
    image=(
        Image.from_registry("nvidia/cuda:12.3.1-runtime-ubuntu22.04")
        .add_python_version("3.11")
        .add_python_packages(["torch==2.3.1"])
        .build_with_gpu("L4")
    ),
    gpu="L4",
    gpu_count=1,
    timeout_seconds=CASE_TIMEOUT_SECONDS,
)
def cuda_torch() -> str:
    subprocess.run(
        [sys.executable, "-c", "import torch; assert torch.cuda.is_available()"],
        check=True,
    )
    return "cuda:True"


@app.function(
    name="pytorch-cuda",
    image=(
        Image.from_registry("pytorch/pytorch:2.3.1-cuda11.8-cudnn8-runtime")
        .add_python_version("3.11")
        .add_commands(["apt-get update && apt-get install -y ffmpeg"])
        .build_with_gpu("L4")
    ),
    gpu="L4",
    gpu_count=1,
    timeout_seconds=CASE_TIMEOUT_SECONDS,
)
def pytorch_cuda() -> str:
    subprocess.run(
        [sys.executable, "-c", "import torch; assert torch.cuda.is_available()"],
        check=True,
    )
    return f"cuda:True:ffmpeg:{shutil.which('ffmpeg') is not None}"


__all__ = ["APP_NAME", "CASE_TIMEOUT_SECONDS", "app", "cuda_runtime", "cuda_torch", "pytorch_cuda"]

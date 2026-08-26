"""Report what a GPU container can actually reach, one layer at a time.

Written because "the GPU works" was believed for as long as nothing checked the
middle of the chain. Devices arrived, the driver did not, and the workload said
so as a CUDA error that read like the author's bug. Each layer below is reported
rather than raised on, so a run that fails names the first layer that is missing
instead of the first call that happened to touch it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path
from typing import TypedDict

from lazycloud import App, GpuType, Image

app = App("gpu-probe")

image = Image(python_version="3.12")


class GpuReport(TypedDict):
    devices: list[str]
    driver_libraries: list[str]
    ld_library_path: str
    libcuda: str
    cuda_devices: str
    nvidia_smi: str


@app.function(name="probe", image=image, cpu=1, memory="2Gi", gpu=GpuType.L4, gpu_count=1)
def probe() -> GpuReport:
    return {
        "devices": sorted(p.name for p in Path("/dev").glob("nvidia*")),
        "driver_libraries": sorted(p.name for p in Path("/usr/local/nvidia/lib64").glob("*"))[:12],
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH", ""),
        "libcuda": _load_libcuda(),
        "cuda_devices": _cuda_device_count(),
        "nvidia_smi": _run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]
        ),
    }


def _load_libcuda() -> str:
    try:
        ctypes.CDLL("libcuda.so.1")
    except OSError as error:
        return f"not loadable: {error}"
    return "loaded"


def _cuda_device_count() -> str:
    """Ask the driver itself, which is the only layer that proves nvproxy works.

    Loading the library only proves a file arrived. cuInit is the first call that
    crosses into the sandbox's driver proxy and reaches the card.
    """
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError as error:
        return f"not loadable: {error}"
    status = cuda.cuInit(0)
    if status != 0:
        return f"cuInit failed with {status}"
    count = ctypes.c_int()
    status = cuda.cuDeviceGetCount(ctypes.byref(count))
    if status != 0:
        return f"cuDeviceGetCount failed with {status}"
    return f"{count.value} device(s)"


def _run(command: list[str]) -> str:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"{command[0]} did not run: {error}"
    if completed.returncode != 0:
        return f"{command[0]} exited {completed.returncode}: {completed.stderr.strip()}"
    return completed.stdout.strip()

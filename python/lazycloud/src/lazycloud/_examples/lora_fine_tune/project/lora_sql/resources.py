"""The app, storage and images that every workload in this project shares."""

import re
from pathlib import Path

from lazycloud import App, GpuType, Image, Volume

app = App("lora_sql")

BASE_MODEL = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
DEFAULT_RUN = "sql-v1"
# The vLLM names of the two models: the base weights and the base plus the adapter.
BASE_MODEL_NAME = "base"
TUNED_MODEL_NAME = "tuned"
# vLLM reserves adapter memory for this rank, so training may not exceed it.
MAX_LORA_RANK = 32
MAX_MODEL_LEN = 2048
MAX_SQL_TOKENS = 256
# A 1.5B model fits either 24 GB card; the list takes whichever is free first.
GPUS = [GpuType.L4, GpuType.A10G]

# Base weights, prepared data, checkpoints and adapters share one volume, so
# every container reads them from storage instead of the network.
STORAGE = Path("/lora-sql")
storage = Volume("lora-sql", str(STORAGE))
BASE_MODEL_DIR = STORAGE / "base-model"
DATA_DIR = STORAGE / "data"
RUNS_DIR = STORAGE / "runs"
RUN_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

cpu_image = Image.from_uv(".")
data_image = Image.from_uv(".", groups=["data"])
# Triton, which vLLM runs its kernels with, compiles a small C launcher on first use.
gpu_image = Image.from_uv(".", groups=["gpu"]).add_commands(
    [
        "apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev "
        "&& rm -rf /var/lib/apt/lists/*"
    ]
)


def run_dir(run: str) -> Path:
    """Where one training run keeps its checkpoints, adapter and settings."""
    if not RUN_NAME.fullmatch(run):
        raise ValueError(f"run names use lowercase letters, digits and dashes, not {run!r}")
    return RUNS_DIR / run


def adapter_dir(run: str) -> Path:
    return run_dir(run) / "adapter"

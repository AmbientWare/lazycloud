"""Serve a small instruction model through vLLM's OpenAI-compatible API.

Deploy from the downloaded project directory. The pod creates or reuses its declared cache:

    uv run lazycloud deploy app:app \
        --resource pod:openai-server

Use the printed URL with a LazyCloud bearer token to call ``/v1/chat/completions``.
Stop GPU charges with ``uv run lazycloud deployment stop openai-server`` when finished.
The full request and cache cleanup are in https://docs.lazycloud.dev/examples/openai-compatible-llm.
"""

from __future__ import annotations

from lazycloud import App, GpuType, Image, Volume

APP_NAME = "openai_compatible_llm"
POD_NAME = "openai-server"
MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
SERVED_MODEL_NAME = "qwen-1.5b"
VLLM_IMAGE = "vllm/vllm-openai:v0.23.0"
MODEL_CACHE_VOLUME = "vllm-model-cache"
MODEL_CACHE_PATH = "/root/.cache/huggingface"
SERVER_PORT = 8000

app = App(APP_NAME)
model_cache = Volume(MODEL_CACHE_VOLUME, MODEL_CACHE_PATH)
image = Image.from_registry(VLLM_IMAGE)

server = app.pod(
    name=POD_NAME,
    image=image,
    command=[
        "vllm",
        "serve",
        "--host",
        "0.0.0.0",
        "--port",
        str(SERVER_PORT),
        "--model",
        MODEL_ID,
        "--served-model-name",
        SERVED_MODEL_NAME,
        "--download-dir",
        MODEL_CACHE_PATH,
        "--max-model-len",
        "8192",
        "--gpu-memory-utilization",
        "0.90",
    ],
    ports={"http": SERVER_PORT},
    env={"HF_HOME": MODEL_CACHE_PATH},
    cpu=4.0,
    memory="16Gi",
    gpu=GpuType.L4,
    gpu_count=1,
    keep_warm=600,
    volumes=[model_cache],
    authorized=True,
)

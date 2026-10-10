"""Serve the base model and the adapter from one vLLM server with an OpenAI-compatible API."""

from .resources import (
    BASE_MODEL_DIR,
    BASE_MODEL_NAME,
    DEFAULT_RUN,
    GPUS,
    MAX_LORA_RANK,
    MAX_MODEL_LEN,
    TUNED_MODEL_NAME,
    adapter_dir,
    app,
    gpu_image,
    storage,
)

# The run whose adapter the server loads. Change it and redeploy to promote another run.
SERVED_RUN = DEFAULT_RUN
SERVER_PORT = 8000

sql_server = app.pod(
    name="sql-server",
    image=gpu_image,
    command=[
        "vllm",
        "serve",
        str(BASE_MODEL_DIR),
        "--served-model-name",
        BASE_MODEL_NAME,
        "--enable-lora",
        "--lora-modules",
        f"{TUNED_MODEL_NAME}={adapter_dir(SERVED_RUN)}",
        "--max-lora-rank",
        str(MAX_LORA_RANK),
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--host",
        "0.0.0.0",
        "--port",
        str(SERVER_PORT),
    ],
    ports={"http": SERVER_PORT},
    health_check_path="/health",
    gpu=GPUS,
    cpu=4,
    memory="16Gi",
    volumes=[storage],
    # Stop the GPU after five idle minutes; the next request starts it again.
    keep_warm=300,
    authorized=True,
)

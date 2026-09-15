# Serve an instruction model through a vLLM API.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run lazycloud deploy app:app
```

This needs an L4 and account credit. The pod creates or reuses its model-cache
volume. Call the printed URL with a LazyCloud bearer token; the guide includes
curl requests. The GPU remains allocated while the service runs.

Stop it with `uv run lazycloud deployment stop openai-server`.
Delete `vllm-model-cache` separately only when you no longer need its contents.

[Full guide](https://docs.lazycloud.dev/examples/openai-compatible-llm)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

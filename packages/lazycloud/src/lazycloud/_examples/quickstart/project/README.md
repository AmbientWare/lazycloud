# Run a Python function on remote compute.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python quickstart.py
uv run lazycloud run quickstart.py:hello LazyCloud
uv run lazycloud deploy quickstart
```

`python quickstart.py` calls `hello` locally, then in the cloud.
`lazycloud run` makes only the cloud call, showing progress and logs before it
prints `hello LazyCloud`. `lazycloud deploy` puts `hello` and the `greet`
endpoint in the cloud and prints the endpoint URL. The image and resource
settings live in `quickstart.py`.

[Full guide](https://docs.lazycloud.dev/getting-started/quickstart)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

# Run a Python function on remote compute.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run lazycloud run quickstart:hello LazyCloud
```

The terminal shows progress and logs, then prints `hello LazyCloud`.
`uv run python quickstart.py` makes the same remote call. Use `hello.local(...)`
for local execution. Edit the image and resource settings in `quickstart.py`.

[Full guide](https://docs.lazycloud.dev/getting-started/quickstart)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

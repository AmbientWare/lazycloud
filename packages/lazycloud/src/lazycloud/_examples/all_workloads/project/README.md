# Explore functions, APIs, schedules, pods, and sandboxes.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run lazycloud deploy app:app
uv run lazycloud run app:run_function 7
```

The result includes a task ID and a calculation result of 49.
Try `app:run_nested_function 6`, `app:run_endpoint 7`, or `app:run_asgi`.
The failure helpers deliberately submit failing tasks and return
`expected_failure: true`.

The heartbeat runs every minute and the pod remains running. Pause them with
`uv run lazycloud app pause all_workloads`. On-demand pod and sandbox helpers
return container IDs; stop those containers separately. Delete the app and
saved artifacts when no longer needed.

[Full guide](https://docs.lazycloud.dev/concepts/apps)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

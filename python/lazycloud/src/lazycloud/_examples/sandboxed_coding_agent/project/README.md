# Plan a code change and test it in an isolated sandbox.

Set `CODING_AGENT_BASE_URL`, `CODING_AGENT_MODEL`, and
`CODING_AGENT_API_KEY` in your environment first. Configuration stores these
values as workspace secrets without printing them. The guide shows masked input.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m sandboxed_coding_agent.configure
uv run lazycloud deploy sandboxed_coding_agent.app:app
uv run lazycloud run sandboxed_coding_agent.app:run_agent
```

The planner sends the prompt and seed files to that provider. The sandbox has
no credentials or network access and terminates after the test. Read
`tests_passed` and `test_output`; CLI success alone does not mean the repair passed.

Delete `plan-patch` and the example's three secrets when no longer needed.

[Full guide](https://docs.lazycloud.dev/examples/sandboxed-coding-agent)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

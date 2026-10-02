# Save a report in a volume and as a downloadable task artifact.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run lazycloud run app:create_report
```

The result includes an artifact ID and filename. Download it from Storage in the
dashboard, or copy the volume file with:

```bash
uv run lazycloud cp lazycloud://artifact-reports/report.txt ./report.txt
```

The declared volume is created on first use. Artifacts and volumes have separate
retention. Remove the returned artifact and the volume when you no longer need them.

[Full guide](https://docs.lazycloud.dev/concepts/artifacts)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

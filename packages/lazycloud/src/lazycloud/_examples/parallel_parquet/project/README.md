# Process Parquet partitions in an existing S3 bucket.

Before running, follow the guide to select an existing bucket, unique input/output
paths, and credential secret names through `LAZYCLOUD_PARQUET_*` variables.
The configuration module reads `PARQUET_S3_ACCESS_KEY_ID` and
`PARQUET_S3_SECRET_ACCESS_KEY` from your environment and stores their values.
With an authorized AWS instance role, omit credential setup and select `pool="aws"`
on the functions instead.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m parallel_parquet.configure
uv run lazycloud run parallel_parquet.app:run_batch true 4 10 false
```

The sample creates four partitions with ten rows each. The result reports
40 rows and the summary key. Use `false` as the first argument for existing
data. Your bucket retains inputs and outputs after containers stop; remove
only the sample keys you created.

[Full guide](https://docs.lazycloud.dev/examples/parallel-parquet-s3)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.

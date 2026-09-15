# Run an example

Choose a guide below and run its commands from this repository's root.
Use Python 3.12 or newer. Set up an environment with the public SDK:

```bash
uv venv .venv-examples --python 3.12
source .venv-examples/bin/activate
uv pip install ./packages/shared ./packages/lazycloud
lazycloud login
```

Keep this environment active. The document-processing example also needs
FastAPI locally; its guide includes the install command. GPU dependencies
install in remote images.

If you use several workspaces, choose one with
`lazycloud workspace use <name>`. For CI, inject `LAZYCLOUD_TOKEN` and
`LAZYCLOUD_WORKSPACE` through the job's environment.

## Choose a workflow

- [Serve a language model](../docs/examples/openai-compatible-llm.mdx) with
  vLLM on an L4, an authenticated API, and a reusable model cache.
- [Train an object detector](../docs/examples/train-yolo-object-detector.mdx)
  and download predictions from its saved checkpoint.
- [Extract text from documents](../docs/examples/document-processing-asgi.mdx)
  through a browser upload app.
- [Test a coding agent's patch](../docs/examples/sandboxed-coding-agent.mdx)
  in a sandbox without network access or provider credentials.
- [Summarize Parquet files](../docs/examples/parallel-parquet-s3.mdx) in your
  bucket with parallel function calls.

Each guide names required credentials, expected output, and cleanup steps.
Workload code defines images, compute, volumes, schedules, and secret names.
Mounted volumes are created on first use and reused by name within the workspace.
The credential examples include Python setup modules; values stay out of source
control. Deploy or run the app to use its definitions.

`lazycloud run` shows function progress, logs, and results. Use task and log
commands separately when investigating earlier runs or background work.

## Save a report as an artifact

`examples/artifacts/app.py` saves a report in a volume and attaches a copy
to the task as an artifact:

```bash
lazycloud run examples.artifacts.app:create_report
```

The function's volume declaration handles storage creation. The result includes
the artifact ID and filename. Open Storage, then Artifacts
in the dashboard to download it. The volume copy stays at `report.txt`:

```bash
lazycloud cp lazycloud://artifact-reports/report.txt ./report.txt
```

When finished, remove the returned artifact with
`lazycloud artifact delete <artifact-id>`. Delete `artifact-reports` only
if you no longer need its files. See [artifact retention](../docs/concepts/artifacts.mdx).

## Try several workload types together

`examples.all_workloads` includes functions, HTTP endpoints, an ASGI app,
a recurring heartbeat, a pod, and an on-demand sandbox. Deploy it when you
want to explore those resources in the dashboard:

```bash
lazycloud deploy examples.all_workloads:app
lazycloud run examples.all_workloads:run_function 7
```

The result includes a task ID and a calculation result of 49. Then try:

```bash
lazycloud run examples.all_workloads:run_nested_function 6
lazycloud run examples.all_workloads:run_artifacts "sample report"
lazycloud run examples.all_workloads:run_background_job 5
lazycloud run examples.all_workloads:run_endpoint 7
lazycloud run examples.all_workloads:run_asgi
```

The background helper submits with `.spawn()` and retrieves the result
before returning. Use `jobs.spawn(...)` directly for a detached call.

To inspect error handling, run `run_function_failure` or
`run_background_job_failure`. They deliberately fail a task and return
`expected_failure: true`. `exercise_runs` runs all function scenarios,
including those intentional failures.

The heartbeat runs every minute and the pod stays running after deployment.
Stop them when finished:

```bash
lazycloud app pause all_workloads
```

The `create_pod` and `create_sandbox` helpers start additional containers
and return their IDs. Stop each returned container with
`lazycloud container stop <container-id>`; pausing the app's deployments
does not replace cleanup of those on-demand instances.

Use `lazycloud app delete all_workloads` to remove the app. Saved artifacts
have separate retention; delete any you no longer need from Storage.

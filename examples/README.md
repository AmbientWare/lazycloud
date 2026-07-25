# Examples

Run examples from the repository root with the public `lazycloud` command.
Authenticate with `lazycloud login`; for non-interactive use, configure the
documented `LAZYCLOUD_ENDPOINT`, `LAZYCLOUD_TOKEN`, and `LAZYCLOUD_WORKSPACE`
settings instead.

## Guided Examples

Each directory below is paired with a complete Mintlify guide. The guide is
the canonical entry point for prerequisites, deployment, inspection, costs or
security boundaries, and cleanup:

- [OpenAI-compatible LLM service](../docs/examples/openai-compatible-llm.mdx)
  — an authenticated vLLM Pod with one GPU and a durable model cache.
- [Train a YOLO object detector](../docs/examples/train-yolo-object-detector.mdx)
  — separate GPU training and prediction Functions sharing a Volume.
- [Document processing with FastAPI](../docs/examples/document-processing-asgi.mdx)
  — an ASGI upload UI backed by durable OCR Tasks.
- [Sandboxed coding agent](../docs/examples/sandboxed-coding-agent.mdx)
  — a trusted planner Function and a network-blocked execution Sandbox.
- [Parallel Parquet processing on S3](../docs/examples/parallel-parquet-s3.mdx)
  — Task Queue fan-out over CloudBucket-mounted partitions.

Run the deployment and invocation commands in a guide from the repository
root. Examples use importable module targets such as
`examples.openai_compatible_llm.app:app`; they do not require changing into the
example directory.

## All Workloads

`examples.all_workloads` is one app that makes every currently deployable
workload kind visible in the dashboard: function, endpoint, ASGI, task queue,
cron, and pod. It also creates an on-demand sandbox through the public SDK;
sandboxes are not deployments.

Deploy all workload kinds:

```sh
uv run lazycloud deploy examples.all_workloads:app --workspace default
```

Create successful, failed, nested, and queued Tasks:

```sh
uv run lazycloud run examples.all_workloads:exercise_runs 7
```

Each Task-producing path is also callable independently:

```sh
uv run lazycloud run examples.all_workloads:run_function 7
uv run lazycloud run examples.all_workloads:run_function_failure 13
uv run lazycloud run examples.all_workloads:run_nested_function 6
uv run lazycloud run examples.all_workloads:run_task_queue 5
uv run lazycloud run examples.all_workloads:run_task_queue_failure 17
```

Exercise inbound HTTP workloads:

```sh
uv run lazycloud run examples.all_workloads:run_endpoint 7
uv run lazycloud run examples.all_workloads:run_endpoint 7 true
uv run lazycloud run examples.all_workloads:run_asgi
```

The cron runs once per minute after deployment. The pod is maintained by its
deployment. The following commands create additional on-demand pod and sandbox
containers when those inventory states are useful:

```sh
uv run lazycloud run examples.all_workloads:create_pod
uv run lazycloud run examples.all_workloads:create_sandbox
```

Endpoint, ASGI, and exposed sandbox-port calls require routable inbound
container networking. A deployment can succeed while those calls remain
unavailable on a direct-transport environment that only publishes private
container bridge addresses.

## Cron Failure Acceptance

`examples.cron_failure` is a disposable App for verifying Cron retry,
terminal failure, and deployment lifecycle behavior without adding a
permanently failing schedule to the All Workloads App.

```sh
uv run lazycloud deploy examples.cron_failure:app --workspace default
```

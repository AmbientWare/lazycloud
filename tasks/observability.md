# Observability packet

Parity section "Logs, events and metrics" from tasks/parity.md, except what
other packets own: `lazycloud logs` and `container list/stop/attach`
(control), `container checkpoint` (workloads) and callbacks (workload
runtime). Migration `migrations/0010_observability.sql`; protobuf fields
80-89.

## Outcome

The dashboard can draw container CPU, memory, network, disk and GPU charts,
workload latency and cold starts, the account metrics drawer, every task
drawer tab that is not logs or result, and live updates without polling.
Server, scheduler and agent log with task, attempt, container, host and
request ids, trace over OTLP when an endpoint is set, and serve `/metrics`.

## Decisions

- Change stream. Statement-level triggers on tasks, containers, apps,
  workloads and secrets call `publish_changes`, which sends one `NOTIFY
  lc_changes` per statement and workspace at commit. The payload is the SSE
  data as is, so the server never re-encodes it per subscriber. A statement
  that would exceed the 8000-byte limit sends grouped changes with a count.
  Other owners publish their resources by calling `publish_changes` from
  their own triggers. Each server holds one LISTEN connection, a ring of the
  last 16,384 events and up to 20,000 streams with 256 queued events each. A
  stream that falls behind, a lost database connection and a Last-Event-ID
  the ring no longer holds all end in a `reset` event: the client reloads. A
  stream ends after 10 minutes so the reconnect authorizes again. PostgreSQL
  queues notifications in commit order for every listener, so an event id
  names the same position on every server. Rolled-back writes publish
  nothing. The triggers observe; they decide nothing.
- Container metrics. The agent finds each container's cgroup v2 directory
  from its init process once, then every 5 s reads `cpu.stat`, `memory.stat`
  (anon plus file_mapped, as the reference), `memory.swap.current`,
  `io.stat` and `/proc/<pid>/net/dev`, and GPUs through `nvidia-smi` for
  containers Docker gave devices. One `ContainerMetrics` message per host
  goes over the session, only while its queue is at most half full. The
  server hands it to a bounded ingest queue (dropped when full, counted on
  `/metrics`) that writes every host's samples in one statement per second,
  keeping only samples from the host the container is assigned to.
  Samples stay an hour. The scheduler folds finished minutes into
  `container_metric_minutes` under a row lock, at most ten minutes per pass,
  and keeps those seven days; deletion never removes an unfolded sample.
  Reads at steps under a minute use samples; longer steps merge minute
  points with samples not yet folded.
- Start stages. The agent reports image, source, create and runtime stage
  times in every container report (field 80); the server stores the first
  report of each for the assigned host. The lifecycle adds placement and
  draining from the container row's own timestamps.
- Timeline and trace come from durable rows only: tasks, attempts and task
  lineage. No event table.
- Performance and aggregates are SQL over the recent-first id indexes,
  bounded by uuidv7 values built from the range, so they read the rows in
  the range and not the history. CPU and memory allocation comes from
  container lifetimes (assignment to stop) found through the live partial
  index and a new stopped-at index.
- Plan limits are the billing packet's: `observability.LimitSource`. Until
  billing wires one, `/v1/me/metrics` reports usage and leaves `limits` out.
- Tracing. `internal/telemetry` builds each binary's tracer provider (no-op
  without `LAZYCLOUD_OTLP_ENDPOINT`), W3C propagation over HTTP and gRPC, a
  correlated slog handler and a Prometheus registry. HTTP spans and metrics
  carry the OpenAPI operation id, not the path. Submit stores the request's
  traceparent on the task; the claim returns it, and the agent records the
  attempt as a span in that trace around its `CompleteTask` call.

## Dashboard operations

| Dashboard item | Operation |
| --- | --- |
| Live updates | `streamChanges` GET `/v1/workspaces/{ws}/changes/stream` (Last-Event-ID; events `change` and `reset`) |
| Container metrics charts | `getContainerMetrics` GET `/v1/workspaces/{ws}/containers/{id}/metrics?start&end&step_seconds` |
| Task drawer lifecycle strip, container tab, stop cause | `getContainerLifecycle` GET `/v1/workspaces/{ws}/containers/{id}/lifecycle`; `getContainer` (control) |
| Trace tab row phases | `listContainerLifecycles` POST `/v1/workspaces/{ws}/containers/lifecycles` |
| Lifecycle timeline | `getTaskTimeline` GET `/v1/workspaces/{ws}/tasks/{task}/timeline` |
| Trace (call graph) | `getTaskCallGraph` GET `/v1/workspaces/{ws}/tasks/{task}/call-graph` |
| Pending notice | `getTask` `pending` (control) |
| Workload performance | `getDeploymentPerformance` GET `/v1/workspaces/{ws}/deployments/{id}/performance?window_seconds&start&end` |
| Tasks and failures over 24 h | `getTaskMetrics` GET `/v1/workspaces/{ws}/metrics/tasks?start&end&app&function` |
| App sparklines, app and workload activity | `getTaskActivity` GET `/v1/workspaces/{ws}/metrics/activity?window_seconds&start&end&app` |
| Account containers and concurrency | `getAccountMetrics` GET `/v1/me/metrics` |
| Account activity by app and range | `getAccountActivity` GET `/v1/me/activity?measure&window_seconds&start&end&limit` |

## Intentional differences

- Event ids are a sequence the triggers assign. A resume that the server
  cannot serve gets a `reset` event; the reference skipped trimmed entries
  silently. A Redis outage lost the reference's changes; here a change is
  published by the transaction that made it.
- One `change` event per committed statement carries a list of changes.
  Topics: apps, deployments, tasks, containers, storage.secrets. Compute,
  volume and usage topics come with those packets.
- Container metrics take a range and step and are downsampled; the
  reference returned the newest 500 raw entries of a Redis stream, minus
  other event types. GPU memory and utilization are measured; the reference
  never filled GPU memory. CPU and memory totals are top-level fields.
  Container root disk usage is not reported: function containers have no
  disk limit in this rewrite, so the reference's disk chart would not draw.
- Workload performance takes one deployment, not a list of stub ids, and
  its status counts cover every task in the bucket.
- The call graph is a flat list, oldest first, that the client nests; at
  most 2,000 nodes with `truncated`. The batch lifecycle call returns the
  lifecycles only, without the reference's percentile and bottleneck
  rollups.
- Lifecycle stages are placement, image, source, create, runtime and
  draining, from durable timestamps and host reports, instead of the
  reference worker's twenty internal phases.
- Account activity derives allocations from container lifetimes, not the
  billing ledger. GPU allocation is zero until containers carry GPUs.
- Account concurrency counts every live container in owned workspaces as a
  CPU container: containers have no GPUs yet.

## Evidence

Owner tests against PostgreSQL 18 and Docker (runc), all with `-race`:
`TestChangeStreamDeliversCommittedChangesOfItsWorkspace`,
`TestChangeStreamResumesAfterLastEventIDOrResets`,
`TestLargeStatementsPublishGroupedChanges`,
`TestContainerAppAndDeploymentChangesArePublished`,
`TestMetricSamplesComeOnlyFromTheAssignedHost`,
`TestRollupFoldsMinutesAndKeepsWhatIsNotFolded`,
`TestLifecycleCombinesTransitionsAndHostStages`,
`TestTaskTimelineFollowsAttemptsAndRetries`,
`TestCallGraphReadsTheWholeGraphFromAnyTask`,
`TestClaimCarriesTheSubmittingTrace`,
`TestDeploymentPerformanceBucketsLatencyAndColdStarts`,
`TestTaskMetricsAndActivity`, `TestAccountMetricsAndActivity`
(internal/observability); `TestChangeStreamOverHTTP`,
`TestObservabilityRoutesAuthorizeAndValidate` (internal/api);
`TestSessionStoresMetricsAndStartStagesOfTheHostsContainers`
(internal/hostsession); `TestAgentReportsContainerMetricsAndStartStages`,
`TestParseGPUs` (internal/agent); `TestLogsCarryCorrelationFieldsAndTrace`,
`TestRequestsExportSpansAndMetrics` against an in-process OTLP collector
(internal/telemetry).

Acceptance, `python/tests/acceptance/test_observability.py`, on a private
stack (server, scheduler, agent, PostgreSQL 18, Garage, registry): a
function burns CPU for 8 s while the change stream reports queued, running
and succeeded; a resumed stream replays; the timeline, lifecycle, metrics
(peak 999 of 1,000 millicores), call graph from any node, batch
lifecycles, performance, task metrics, activity and account reads all match.
Passed in 12.3 s. Stopping the server with a stream open exits in 0.02 s.

## Measurements

One host, 24 CPUs, PostgreSQL 18 in Docker.

| Scenario | Result |
| --- | --- |
| 1,000 SSE streams on one workspace, one submit | delivered to all 1,000 1.3 to 1.8 ms after the submit returned (p99 of the 1,000 3.2 to 5.7 ms from the request start) |
| Hub hand-off of one event to 1,000 subscribers (2,000 open) | 0.5 ms, no allocation |
| Memory per stream (client and server together) | 53 KiB |
| Database cost of the stream | one LISTEN connection per server; one NOTIFY per statement, whatever the subscriber count |
| Change trigger cost on submit | 1 task: p50 0.52 ms vs 0.37 ms without; 2,000 tasks: 85 vs 84 ms |
| Ingest, 1,000 containers on 20 hosts, one 5 s tick | one statement, mean 19.7 ms, worst 50 ms over an hour; 437 WAL bytes per sample |
| Samples kept for 1,000 containers | 139 MiB for the hour of 5 s samples |
| Rollup of ten minutes for 1,000 containers | 190 ms per pass |
| Agent sampling of one container | 156 µs; 0.8 ms per pass for 3 containers on the stack |
| One container's hour, one container's week | 1.4 ms, 0.4 ms |
| 24 h aggregates over 300,000 tasks (20,000 in the last day) and 30,000 stopped containers | task metrics 17 ms, activity 7.5 ms, deployment performance 12 ms, account starts 1.2 ms, CPU allocation 1.7 ms |
| Dashboard reads over HTTP on the stack | p50 0.5 to 1.9 ms each |

Idle cost: the scheduler's rollup pass runs every 30 s and reads the
samples written since the last one; the agent reads files of running
containers only; the change stream does nothing without commits.

## Proposed shared changes

- `Propose: store each task's submitting trace and return it with the
  claim` (execution): `tasks.traceparent` written by `InsertTasks`, read by
  `ClaimQueuedTasks`, `ClaimedTask.TraceParent`.
- Wiring edits: `api.Owners` gains `Observability` and `Changes`;
  `hostsession.Config` gains `Observability`; host calls log with host,
  container and attempt ids; the three mains build telemetry; the API test
  env and host session harness run the new owners.
- `go.mod` adds otel, otel/sdk, otlptracegrpc, otelhttp, otelgrpc and
  prometheus/client_golang as direct dependencies.

## Gaps

- GPU sampling through `nvidia-smi` is unverified: no GPU host here, and
  containers do not get GPUs yet.
- Plan limits wait for billing's `LimitSource`.
- The web packet replaces the dashboard's reference endpoints with these
  operations.
- Container root disk usage is not sampled.
- Sampling needs cgroup v2 and an agent that sees host `/proc`; gVisor
  containers are unverified.
- The 1,000-stream fan-out ran in one process with its clients; a real
  reverse proxy in front was not measured.

## Try it

Telemetry is off unless configured: `LAZYCLOUD_OTLP_ENDPOINT` (with
`LAZYCLOUD_OTLP_INSECURE=true` for a local collector),
`LAZYCLOUD_TRACE_SAMPLE_RATIO`, `LAZYCLOUD_METRICS_ADDR` per binary and
`LAZYCLOUD_LOG_FORMAT=text|json`. Start `deploy/local/run.sh start`, export
its environment, then run
`LAZYCLOUD_TEST_ENDPOINT=… LAZYCLOUD_TEST_TOKEN=… LAZYCLOUD_TEST_WORKSPACE=dev
uv run --group dev pytest -x -s python/tests/acceptance/test_observability.py`.
`curl -N -H "Authorization: Bearer $LAZYCLOUD_TOKEN"
$LAZYCLOUD_ENDPOINT/v1/workspaces/dev/changes/stream` shows the stream. The
measurements run with `LAZYCLOUD_MEASURE=1 go test -run 'TestMeasure' -v`
in internal/observability and internal/api.

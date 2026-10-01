# Endpoints packet

Parity sections: "Endpoints, ASGI and realtime", "Custom domains", and
`lazycloud serve` with `App.serve`/`Function.serve`/`Endpoint.serve` from "CLI
misc". Migration `0006_endpoints.sql`, protobuf field range 30-39.

## Outcome

`lazycloud deploy api_demo:count_words` prints an HTTPS URL; a request with a
bearer token reaches a container through the edge, cold or warm, and the
container scales back to zero after `keep_warm`. ASGI apps stream (SSE) and
upgrade to WebSockets; realtime handlers answer per message. `.request()` and
`.target()` reach the preview or the deployment. `lazycloud serve` runs a
preview container, syncs edits into it and prints its output. `lazycloud
domain add|status|list|remove` registers account hostnames with Cloudflare for
SaaS, and the edge routes only verified ones.

## Design

- Definition. An endpoint is a `FunctionSpec` with an `http` section (kind
  `endpoint`, `asgi` or `realtime`, route, methods, domain, authorized,
  workers). Workload kinds `endpoint` and `asgi` (realtime is asgi). Control
  mints the deployment subdomain `<stem>-<8hex>` from (workspace id, app, name,
  kind) exactly as the reference, and claims it with the custom hostname in
  `http_routes` inside the deploy transaction, so collisions and hostnames
  another deployment serves reject the deploy.
- Edge (`internal/edge`, in the server binary, own listener). Host labels
  resolve from an in-memory route table: `<subdomain>`, `-latest`, `-vN`,
  `<release id>` (the reference's stub URL), `<container id>` and verified
  custom hostnames. Triggers in 0006 send `lc_route` when workloads, apps,
  routes or domains change and `lc_endpoint` (workload id) when a container
  of the workload changes state; the edge refreshes from them and rebuilds
  after a listener reconnect. It authenticates with identity (cached 5 s per
  token and workspace), strips the platform bearer token, sets
  `X-Forwarded-*`, and forwards each request as its own gRPC stream.
- Data connection. `HostData` (contracts/host/v1/data.proto) is separate
  from the control session. The agent keeps idle `Forward` streams open; the
  edge assigns one per request and asks for more on `Listen` when it runs
  out. Bodies stream in both directions under gRPC per-stream flow control.
  A 101 response turns the stream into a raw byte tunnel for WebSockets.
- Host. The agent dials the supervisor's HTTP socket in the container's link
  directory. The supervisor runs `workers` runner processes, each serving HTTP
  on a listening socket it inherits, admits at most `concurrency` requests per
  worker and answers `503` with `Lazycloud-Busy` when full or draining; the
  agent turns that into an explicit busy result and the edge retries another
  container with the buffered body (up to 64 KiB).
- Runner. `load` gains `http` (kind); the runner serves the handler with
  uvicorn on `LAZYCLOUD_HTTP_FD`. Endpoints map the JSON body and query to
  arguments and map results exactly as the reference.
- Execution. No task per request. Each edge keeps in-flight and waiting
  counts per release in memory and upserts `endpoint_loads` leases (demand,
  keep-warm window peak, TTL 15 s); a cold request publishes at once and wakes
  planning. `PlanServing` sizes HTTP releases from the leases:
  `clamp(ceil(max(demand, window peak) / tasks_per_container), min, max)`. A
  replaced release keeps its containers until the active release has a ready
  one, then drains; pinned `-vN` traffic keeps its own demand. Cold requests
  wait for the container-ready NOTIFY up to the request deadline.
- Previews. A preview is a release with a negative version from the
  `preview_versions` sequence, with a `previews` lease renewed while the CLI
  follows its output. Working-tree releases keep a null version. Planning keeps exactly one
  container while the lease lives. File sync goes over the data connection to
  the agent, which writes the workspace and tells the supervisor to restart
  its runners after in-flight work.

## Plan

1. Contracts: OpenAPI, data.proto, StartContainer/Configure fields, runner
   `load.http`, migration 0006.
2. Control: HTTP specs in deploy, route claims (Propose commits).
3. Runner HTTP mode, supervisor HTTP mode, agent data client, edge, server
   wiring; deploy and request an endpoint end to end.
4. Execution leases and endpoint planning; cold start, scale to zero, drain
   on redeploy.
5. ASGI, SSE, WebSockets, realtime; HTTP function invoke.
6. SDK `deploy`/`request`/`target`, URLs in deploy output.
7. Custom domains with the Cloudflare provider.
8. Previews and `lazycloud serve`.
9. Measurements.

## Progress

- [x] 1 Contracts
- [x] 2 Control
- [x] 3 Host path and edge
- [x] 4 Planning
- [x] 5 ASGI, WebSockets, function invoke
- [x] 6 SDK
- [x] 7 Custom domains
- [x] 8 Serve
- [x] 9 Measurements

## Intentional differences

- The edge never passes a platform token to a workload; the reference
  passed the caller's to user code. A public workload keeps any other
  Authorization header, and the SDK sends no token to a public target.
- No task per endpoint or ASGI request. Each gets an `X-Request-Id`, sent to
  the workload and returned, a record (status, latency, bytes, container)
  written in batches and kept seven days, and the output the handler wrote
  while serving it (`GET .../apps/{app}/requests`, `.../requests/{id}`,
  `.../requests/{id}/logs`, `lazycloud requests list|logs`). The reference
  showed requests as tasks with `X-Task-Id`; functions invoked over HTTP are
  still tasks and still return `X-Task-Id`. Calls an endpoint makes are not
  children of its request, as they were of the reference's request task.
- The reference SDK's `/api/v1/functions/invoke/stream` NDJSON call was its
  internal transport for `.remote()`, not a public HTTP route: no doc or
  other consumer used it. Task submit plus the task log stream replace it,
  so it is dropped.
- A function preview is a release of the function; local calls target it
  through the function's submit with `release_id`, which admits while the
  preview's lease lives.
- A deleted workload frees its subdomain and hostname, so redeploying the
  same name claims the same URL.
- Endpoint and ASGI specs reject `cron` and `in_process`; HTTP workers admit
  concurrent requests themselves.
- `latest` falls back only to an older deployed release while the active
  one starts, never to a newer one rolled back from.
- With several servers an edge relays a request for a host whose data
  connection another server holds over `EdgeRelay` (cluster-internal
  listener, per-edge token in the `edges` table); container choice,
  admission and the response stay with the edge the client reached.
- A workload's token policy is its active release's: once it goes private,
  pinned `-vN`, release-id and container hosts of older public releases
  require a token too.
- Records and the output containers wrote are kept seven days, pruned in
  batches by every edge.

## Handed off

- `checkpoint_enabled` and endpoint shells belong to the workloads packet;
  the SDK rejects them on HTTP workloads until it lands them.

## Gaps

- The Team/Business plan gate on custom domains needs billing.
- The dashboard's view of request records and logs is the web packet's; the
  API and CLI serve them now.
- Relays are verified with two edges in one process; the Helm
  chart binds the relay to the pod IP (IPv4), advertises it, exposes it
  through no Service and admits only server pods to it, unverified on a
  cluster.
- Billing's hand-offs (AdmitCustomDomain on domain create, RecordEgress per
  workspace) wait for billing #424.
- gVisor needs `--host-uds` for the supervisor's HTTP socket; only runc is
  verified.
- The reference docs say a domain may be a subdomain of a registered name;
  its code and this rewrite require an exact match.

## Evidence

Local, `go test -race`, real PostgreSQL, garage, registry and Docker (runc):

- `TestEndpointColdWarmAndScaleToZero`: cold request through the edge 1.1-1.6 s;
  warm p50 1.4-1.6 ms, p95 1.8-2.4 ms; no container 3.7-3.9 s after the last
  request with keep_warm 2 s.
- `TestWarmLatencyThroughTheEdgeAndDirect`: 500 sequential, edge p50 1.3 ms /
  p95 1.6 ms against the supervisor socket direct p50 0.36 ms / p95 0.62 ms.
- `TestEndpointServesAThousandConcurrentRequests`: 1000 concurrent 10 ms
  requests on up to 2 containers of capacity 32 in 0.52-0.62 s, p50 275-400
  ms, p95 450-574 ms.
- `TestASGIStreamsUploadsUpgradesAndStripsTheToken`: first SSE event after
  143 us, 3 events over 0.9 s; uploads, WebSocket upgrade, token stripped.
- `TestEndpointQueuesPastCapacityAndRejectsPastMaxPending` (429),
  `TestRedeployKeepsServingUntilTheNewReleaseIsReadyThenDrains`,
  `TestEndpointThatFailsToLoadAnswersWithItsError` (1.1-1.3 s),
  `TestFunctionsAreInvokedOverHTTP`, `TestEndpointGetsItsSecretsAndRunsOnStartFirst`.
- `TestServePreviewSyncsSourceAndStops`: preview ready 1.3 s, sync to the
  edited answer 1.2-1.4 s; `TestFunctionPreviewTakesTasksAndLapsesWithoutAFollower`.
- Control: `TestDeployEndpointResolvesHTTPDefaultsAndClaimsItsSubdomain`,
  subdomain digests against the reference. Supervisor: `internal/supervisor/http_test.go`.
  Runner: `python/runner/tests/test_http.py`. SDK:
  `test_deploy_maps_endpoint_and_asgi_options_to_http_specs`.
- Public workloads, volumes, records and relays:
  `TestPublicWorkloadsAnswerWithoutAToken`, `TestEndpointMountsItsVolumes`,
  `TestEndpointRequestsAreRecordedWithTheirOutput`,
  `TestRequestRelaysToTheEdgeHoldingTheHost` (warm through a relaying edge
  p50 2.0 ms, p95 2.8 ms).
- Cloudflare for SaaS, once against the real zone with the development
  credentials (2026-10-01), through `lazycloud domain` on a private stack:
  `add lc-acceptance-c006cbf0.lazycloud-test.invalid` was refused by
  Cloudflare (1409, reserved TLD) as `invalid_request`, recording nothing;
  `add lc-acceptance-0aa44784.lazycloud-acceptance-0aa44784.com` created a
  pending custom hostname and printed the CNAME and `_cf-custom-hostname`
  TXT records, `validating` from the create response's SSL `initializing`;
  `status` and `list` then read `awaiting verification` (SSL
  `pending_validation`), the reference's mapping. `remove` deleted it, and
  the Cloudflare API listed no `lc-acceptance-` hostname afterwards; the zone
  kept only its one pre-existing hostname.
- Review regressions: `TestBrokenResponseReleasesItsCapacity`,
  `TestClientThatLeavesCancelsTheRequest`,
  `TestStalledRequestBodyDoesNotHoldCapacity` and the unit tests in
  `internal/edge/edge_test.go`; `TestUnbuildableStartFailsOnlyItsContainer`.
- Real CLI on a private stack: `lazycloud deploy` prints the URLs; curl with a
  token answers, without one 401; `lazycloud serve api_demo:count_words`
  prints the reference's output, URL after 1.8 s, edit answered 1.35 s after
  save, Ctrl-C stops it; `lazycloud run` during a function serve ran on the
  preview release.

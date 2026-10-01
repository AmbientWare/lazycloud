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
  planning. `PlanEndpoints` sizes HTTP releases from the leases:
  `clamp(ceil(max(demand, window peak) / tasks_per_container), min, max)`. A
  replaced release keeps its containers until the active release has a ready
  one, then drains; pinned `-vN` traffic keeps its own demand. Cold requests
  wait for the container-ready NOTIFY up to the request deadline.
- Previews. A preview is a release with no version, with a `previews` lease
  renewed while the CLI follows its output. Planning keeps exactly one
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

- [ ] 1 Contracts
- [ ] 2 Control
- [ ] 3 Host path and edge
- [ ] 4 Planning
- [ ] 5 ASGI, WebSockets, function invoke
- [ ] 6 SDK
- [ ] 7 Custom domains
- [ ] 8 Serve
- [ ] 9 Measurements

## Intentional differences

(recorded as they are decided)

## Evidence

(measurements and test names)

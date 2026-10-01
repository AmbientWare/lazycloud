# Workload API unification

Wave 3 packet. One `WorkloadSpec` and one resource path for every workload
kind. CLI commands and output, the SDK API and dashboard pages stay as they
were; only the HTTP API and the code behind it change.

## Design

- `WorkloadSpec` replaces `FunctionSpec`. It has a required `kind`
  (`function`, `endpoint`, `asgi`, `pod`, `sandbox`) and the existing `http` and
  `pod` sections. Control rejects a kind that disagrees with the sections. A
  devbox is a `pod` with `pod.kind` devbox; a realtime app is an `asgi` with
  `http.kind` realtime. The dashboard's `/workloads/$kind/$name` routes already
  used these kinds.
- `/v1/workspaces/{ws}/apps/{app}/workloads/{kind}/{name}` addresses a deployed
  workload. A working-tree-only workload has no active release, so it is not
  addressable, which matches the listing.
- `getWorkload` answers `WorkloadDetail{workload, release, http?, schedule?}`.
  That one read replaces the function, endpoint, ASGI and deployment reads.
- Kind-specific routes sit under the path: `function/{name}/tasks`,
  `function/{name}[/versions/{v}]/invoke`, `endpoint|asgi/{name}[/versions/{v}]/invoke/...`,
  `pod/{name}/devbox[/start|/stop]` and `pod/{name}/ssh`.
- `POST /apps/{app}/releases` takes the spec, whose kind and name pick the
  workload, like deploys and previews do.
- The database already had one `workloads` table keyed by `(app_id, kind,
  name)`, so there is no migration.

## Routes

| Old | New |
| --- | --- |
| GET /deployments | GET /workloads (adds kind, id filters) |
| GET /deployments/{id}, GET /apps/{app}/functions/{f}, GET /apps/{app}/endpoints/{n}, GET /apps/{app}/asgi/{n} | GET /apps/{app}/workloads/{kind}/{name} |
| DELETE /deployments/{id}, POST …/stop, …/start, …/scale | same verbs on /apps/{app}/workloads/{kind}/{name}[/stop\|/start\|/scale] |
| GET /deployments/{id}/versions, …/logs, …/performance | /apps/{app}/workloads/{kind}/{name}/versions, /logs, /performance |
| GET /containers?deployment=, ?app=&function= | GET /apps/{app}/workloads/{kind}/{name}/containers |
| POST /apps/{app}/functions/{f}/tasks | POST /apps/{app}/workloads/function/{name}/tasks |
| POST /apps/{app}/functions/{f}[/versions/{v}]/invoke | POST /apps/{app}/workloads/function/{name}[/versions/{v}]/invoke |
| /apps/{app}/endpoints/{n}/…/invoke, /apps/{app}/asgi/{n}/…/invoke | /apps/{app}/workloads/endpoint\|asgi/{name}/…/invoke |
| POST /apps/{app}/functions/{f}/releases | POST /apps/{app}/releases |
| /deployments/{id}/devbox[/start\|/stop] | /apps/{app}/workloads/pod/{name}/devbox[/start\|/stop] |
| /apps/{app}/pods/{pod}/ssh | /apps/{app}/workloads/pod/{name}/ssh |

No old route remains.

## Intentional differences

- `--json` dumps of API models follow the renamed fields. `deploy --json`
  shows `releases[].name` instead of `function`, and workload dumps no longer
  carry `active_release: null`. Human output is unchanged.
- An unknown kind in an API path is a 400 from schema validation. The
  dashboard checks the kind against the generated `WorkloadKind` values first
  and shows its not-found page.
- Releases stored before this change have no `kind` in their spec. Nothing of
  the rewrite is deployed, so a local stack from before it needs a reset
  (`docker compose down -v` and a fresh `.lazycloud`); there is no
  compatibility code. Invoke paths come from the workload row's kind, not the
  stored spec.

## Evidence

- Handwritten lines, excluding generated code and tests, before and after:
  Go 62727 to 62687, SQL 5292 to 5286, Python 37532 to 37503, web 35514 to 35441.
  The total went from 141065 to 140917. The contract went from 8365 to 8324 lines.
- `./check.sh` passes.
- `go test -race` passes for api, control, edge, observability, schedules,
  hostsession, execution, compute, agent and acceptance.
- `pytest -x` passes for python/lazycloud/tests (non-live) and python/shared/tests
  (345 tests). Web vitest passes (116 tests).
- On a private local stack, the CLI deployed the all-workloads example. It ran
  a function, an endpoint and an ASGI app, then ran `deployment
  list/scale/stop/start` by name, id and `NAME-v1`, `logs --deployment`,
  `container list` and `app export`. The old routes answer 404.
- A Playwright run loaded the dashboard's app page and the pages for function,
  endpoint, ASGI, pod and cron. It invoked a function and an endpoint from the
  playground, opened the task, scaled the pod and saw the not-found page for a
  wrong kind, with no 5xx or page errors.

## Gaps

- Devbox start, stop and SSH were not exercised end to end because disks need
  a root agent. They are covered by the updated owner and API tests. The
  live `test_ssh_config_makes_plain_ssh_reach_a_devbox` fails locally for the
  same reason.
- Live tests that failed for reasons this packet does not touch: artifact
  uploads from containers (the presigned URL points at host loopback), hook
  log lines joined in `test_workload_runtime_end_to_end`, and account activity
  grouping in `test_observability_end_to_end` when the workspace has other apps.

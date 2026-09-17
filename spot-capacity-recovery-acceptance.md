# Spot recovery acceptance

Implementation is on `feat/spot-capacity-recovery`. Production has not been
deployed with this change. The live AWS interruption scenario, interrupted-upload
cleanup proof and deployed database-rate verification remain open.

Local owner checks use real PostgreSQL and Redis. Provider-owner tests use their
existing deterministic provider; they do not prove AWS behavior.

## Release cutover

The owner approved a shutdown and replacement of old workers for this release.
Mixed versions are not supported for the image-attempt transition. Old workers
report their container ID as the build ID; retries require separate identities.
Stop old API and scheduler replicas before migration so they cannot create builds
without attempt ownership. Resume on the new release and verify every serving
worker's image and agent identity before running acceptance work.

Production inspection before cutover found no active image builds or containers.
Preserve the database and all existing tenant data. Pause Argo reconciliation
before scaling old workloads down, then resume its normal deployment of the
merged release. Record the deployed revision and public execution result below.

The preceding deployment was blocked by a revoked bootstrap credential despite
an active administrator account. Deployment initialization now checks published
bootstrap and current administrator standing. It preserves token revocation and
still provisions workspace storage. Fifteen identity and offline CLI checks
passed, including worker credential creation after bootstrap-token revocation
and refusal when no active administrator remains.

- Compute, pool drain and migration checks passed, including simultaneous loss
  with one or two replacement markets, a fresh service/Redis client between
  passes, and a rejected purchase releasing ownership before another market
  replaces it. A separate warm-floor check confirms
  two workers occupy distinct eligible availability zones.
- Agent interruption, shutdown, scheduler preemption/liveness, image dispatch,
  worker API and SDK image checks passed in their changed scopes.
- The build retry check exercises production submission and scheduler owners,
  preserves the public build ID and log order, rejects retired execution and
  publication, preserves the successor during cleanup, and fences cancellation.
- Changed-file Python types and Ruff passed. Web TypeScript passed.
- All checks on implementation commit `2ecd22ae5` passed in CI, including the
  changed Python owners, types, lint/format, client wheel installation and web.
- Final review moved source capacity reduction ahead of recovery expiry so an
  expired recovery still prevents replenishment in the threatened market.
  Quota, provider availability and join-authority failures stop recovery after
  releasing the failed purchase. The simultaneous-recovery owner check passed
  all four cases, including quota failure without another purchase.

## Public execution

An isolated canonical Compose project built and activated commit `2ecd22ae5`,
including the API, scheduler, agent and worker. The active release's worker digest
matched the running worker. PostgreSQL, Redis, Garage, the registry and gVisor
were real services. The existing development stack was not modified.

A real `lazycloud run` built a Python 3.12 image in 9.5 seconds, delivered actual
build and function logs, and returned `49` from `square(7)` in 1.9 seconds. Build
`08d6a460-bcfe-44c4-854c-8562a3778947` completed with execution container
`9c6cb99d-4078-4c4e-8933-80e07e157d1a`. Reconnecting with a cursor beyond the last
log returned the successful terminal event. No active apps remained afterward.
The acceptance Compose containers, worker, volumes, network and test PostgreSQL/
Redis containers were removed. No AWS resources were created or interrupted.

This execution exposed and fixed a worker progress report that still equated
the build ID with its container ID. Scheduler wake reads also hit Redis's socket
timeout at the five-second interval; bounded reads retain that reconciliation
interval and permit prompt shutdown. The corrected local scheduler reported no
wake timeout warnings.

The existing `tests.e2e.local.image_build.python_package --case python311-httpx`
scenario built and published its image, then failed because its `tests` module
was excluded from the source bundle. The successful CLI run used a temporary
ordinary application module. Neither execution exercised AWS notices or a real
interrupted upload.

## Database measurements

Measured production owner calls against an isolated PostgreSQL database with
10,000 completed builds and 10,000 completed recovery records. Returned bytes
sum PostgreSQL result values, excluding protocol framing. Counts include session
setup commands. The baseline image method comes from commit `02380bddc` and runs
against the same database and mapper, including the new ownership columns.

| Build polling | Baseline commands / bytes | Current commands / bytes |
| --- | ---: | ---: |
| Pending, no new logs | 5 / 3,944 | 3 / 151 |
| Running, one new log | 5 / 3,988 | 3 / 195 |

An idle recovery pass uses two database commands and returns zero rows/bytes.
At the five-second fallback interval, two scheduler replicas add 48 commands per
minute while idle. Wake notifications can add passes during active recovery.
An image stream polls four times per second, so pending polling is 12 commands
and 604 returned bytes per second per subscriber, versus 20 and 15,776 before.

With 10,000 completed recovery records present during each call, the two-warning
owner scenario produced the following counts. These include test-fixture
savepoint commands and use the deterministic provider. They are local owner
measurements, not deployed rates.

| Replacement markets | Admission | Pending | Fulfillment | Completion |
| --- | ---: | ---: | ---: | ---: |
| One | 327 / 81,375 B | 129 / 37,316 B | 114 / 32,601 B | 42 / 10,108 B |
| Two | 352 / 79,536 B | 162 / 40,931 B | 140 / 36,800 B | 42 / 10,108 B |

Sharing target reconciliation within a pass reduced the one-market pending
pass from 166 commands / 49,181 bytes to 129 / 37,316. Due-row claiming prevents
another replica from executing the same obligations concurrently; it still pays
for its idle claim query. Provider calls and admission are bounded by live work.
With a fresh compute service and Redis client after admission, then a confirmed
capacity rejection in one replacement market, rejection/release used 189
commands / 52,539 bytes. Readmission used 177 / 44,307, fulfillment 114 / 32,594,
and completion 42 / 10,105. The same 10,000 completed records were present.
Both replacements completed without exceeding the four-machine cap or retaining
the failed operation's capacity ownership. This does not simulate a process
crash during a provider call. Production query insights remain unverified.

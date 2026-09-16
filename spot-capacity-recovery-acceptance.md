# Spot recovery acceptance

Implementation is on `feat/spot-capacity-recovery`. Production has not been
deployed with this change. The live AWS interruption scenario, public CLI run,
artifact cleanup proof and deployed database-rate verification remain open.

Local owner checks use real PostgreSQL and Redis. Provider-owner tests use their
existing deterministic provider; they do not prove AWS behavior.

- Compute, pool drain and migration checks passed, including simultaneous loss
  with one or two replacement markets. A separate warm-floor check confirms
  two workers occupy distinct eligible availability zones.
- Agent interruption, shutdown, scheduler preemption/liveness, image dispatch,
  worker API and SDK image checks passed in their changed scopes.
- The build retry check exercises production submission and scheduler owners,
  preserves the public build ID and log order, rejects retired execution and
  publication, preserves the successor during cleanup, and fences cancellation.
- Changed-file Python types and Ruff passed. Web TypeScript passed.

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
Rejected-acquisition/restart cost and production query insights still need
measurement before the full acceptance criteria are satisfied.
